"""Bounded recording; this module never sends audio or changes a telephone call.

The HTTP application must authenticate the WebSocket upgrade before calling
``handle``. A one-use, in-memory ticket additionally binds Twilio's start event
to the intended account and caller leg. All filesystem work runs on a recorder
thread; the event loop only validates and queues bounded messages.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import json
import logging
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import threading
import time
import wave
from typing import Any

SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
STREAM_SID = re.compile(r"MZ[0-9a-fA-F]{32}\Z")
TRACKS = ("inbound", "outbound")
MAX_MESSAGE_BYTES = 65_536
MAX_FRAME_BYTES = 8_000
QUEUE_FRAMES = 256
SILENCE_BLOCK = bytes(65_536)
MAX_TICKETS = 4_096
TICKET_TTL = 120
TOMBSTONE_TTL = 24 * 60 * 60
MIN_FREE_BYTES = 64 * 1024 * 1024
FLUSH_TIMEOUT_SECONDS = 10
logger = logging.getLogger("uvicorn.error")


def _decode_sample(value: int) -> bytes:
    value = ~value & 0xFF
    magnitude = (((value & 15) << 3) + 132) << ((value >> 4) & 7)
    sample = (132 - magnitude) if value & 128 else (magnitude - 132)
    return sample.to_bytes(2, "little", signed=True)


MULAW_PCM = tuple(_decode_sample(value) for value in range(256))


def decode_mulaw(data: bytes) -> bytes:
    """ITU G.711 μ-law to signed, little-endian PCM16; no codec dependency."""
    return b"".join(MULAW_PCM[value] for value in data)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a timestamp or counter")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 12:
        return int(value)
    raise ValueError("Expected an integer")


class CaptureRejected(ValueError):
    """Invalid, expired, or already-consumed capture ticket."""


@dataclass
class CaptureTicket:
    token: str = field(repr=False)
    stream_name: str
    call_sid: str
    created: float = field(default_factory=time.monotonic)
    ended: bool = False
    ended_at: float = 0
    stream_sid: str = ""
    session: _Recorder | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _Frame:
    track: str
    chunk: int
    timestamp_ms: int
    payload: bytes = field(repr=False)


def _private_directory(path: Path) -> int:
    """Open/create the absolute storage directory without following symlinks."""
    if not path.is_absolute() or path == Path(path.anchor) or ".." in path.parts:
        raise ValueError("Capture storage must be an absolute, normalized path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=parent)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        os.fchmod(parent, 0o700)
        return parent
    except BaseException:
        os.close(parent)
        raise


def _observe(observer, method: str, *args):
    """Optional partners cannot break capture, even when their callback fails."""
    if observer is not None:
        try:
            getattr(observer, method)(*args)
        except Exception:
            pass


class _Recorder:
    def __init__(self, settings, ticket: CaptureTicket, websocket, observer=None):
        self.settings = settings
        self.ticket = ticket
        self.websocket = websocket
        self.observer = observer
        self.observer_finished = False
        self.started_at = _now()
        self.started_monotonic = time.monotonic()
        self.frames: queue.Queue[_Frame] = queue.Queue(maxsize=QUEUE_FRAMES)
        self.closing = threading.Event()
        self.done: Future = Future()
        self.closed_event = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.done.add_done_callback(self._observer_finished)
        self.finish_reason = "socket-disconnected"
        self.counters = {"media_messages": 0, "rejected_messages": 0, "dropped_messages": 0}
        self.last_chunk = {track: 0 for track in TRACKS}
        self.last_timestamp = {track: -1 for track in TRACKS}
        self.tracks = {
            track: {"file": f"{track}.wav", "meaning": meaning, "frames": 0,
                    "samples": 0, "gap_samples": 0, "first_timestamp_ms": None,
                    "last_timestamp_ms": None, "last_chunk": None}
            for track, meaning in zip(TRACKS, ("caller-input", "caller-playback"))
        }
        self.thread = threading.Thread(target=self._write, name="twilio-capture", daemon=True)
        self.thread.start()

    def _observer_finished(self, future):
        try:
            self.loop.call_soon_threadsafe(self._finish_observer)
        except RuntimeError:
            pass

    def _finish_observer(self):
        if not self.observer_finished:
            self.observer_finished = True
            _observe(self.observer, "finish", self.ticket.call_sid, self.finish_reason)

    def media(self, event: dict) -> str | None:
        """Queue a validated frame; return a reason when capture must stop."""
        if self.closing.is_set() or self.done.done():
            return "recorder-closed"
        self.counters["media_messages"] += 1
        try:
            if event.get("streamSid") != self.ticket.stream_sid:
                raise ValueError("Wrong stream")
            media = event["media"]
            track = media["track"]
            if track not in TRACKS:
                raise ValueError("Unknown track")
            chunk = _integer(media["chunk"])
            timestamp = _integer(media["timestamp"])
            encoded = media["payload"]
            if (chunk <= self.last_chunk[track] or timestamp < self.last_timestamp[track]
                    or timestamp < 0 or not isinstance(encoded, str)
                    or len(encoded) > ((MAX_FRAME_BYTES + 2) // 3) * 4):
                raise ValueError("Invalid frame order or size")
            payload = base64.b64decode(encoded, validate=True)
            if not payload or len(payload) > MAX_FRAME_BYTES:
                raise ValueError("Empty or oversized frame")
            if timestamp * 8 + len(payload) > self.settings.media_max_seconds * 8_000:
                return "duration-limit"
            frame = _Frame(track, chunk, timestamp, payload)
        except (ValueError, TypeError, KeyError, binascii.Error):
            self.counters["rejected_messages"] += 1
            return None
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            self.counters["dropped_messages"] += 1
            return "queue-overflow"
        self.last_chunk[track] = chunk
        self.last_timestamp[track] = timestamp
        _observe(self.observer, "offer", self.ticket.call_sid, track, timestamp, payload)
        return None

    def _write_frame(self, outputs, frame: _Frame):
        track = self.tracks[frame.track]
        gap = frame.timestamp_ms * 8 - track["samples"]
        if gap < 0:
            self.counters["rejected_messages"] += 1
            return
        if gap and shutil.disk_usage(self.settings.media_storage_dir).free < (
                MIN_FREE_BYTES + (gap + len(frame.payload)) * 2):
            self.finish_reason = "low-disk-space"
            self.closing.set()
            self.counters["dropped_messages"] += 1
            return
        output = outputs[frame.track]
        padding_bytes = gap * 2
        while padding_bytes:
            size = min(padding_bytes, len(SILENCE_BLOCK))
            output.writeframesraw(SILENCE_BLOCK[:size])
            padding_bytes -= size
        output.writeframesraw(decode_mulaw(frame.payload))
        track["frames"] += 1
        track["samples"] += gap + len(frame.payload)
        track["gap_samples"] += gap
        if track["first_timestamp_ms"] is None:
            track["first_timestamp_ms"] = frame.timestamp_ms
        track["last_timestamp_ms"] = frame.timestamp_ms
        track["last_chunk"] = frame.chunk

    def _manifest(self, status: str) -> dict:
        return dict(schema_version=1, call_sid=self.ticket.call_sid,
                    stream_sid=self.ticket.stream_sid, account_sid=self.settings.account_sid,
                    started_at=self.started_at, finished_at=_now(), status=status,
                    finish_reason=self.finish_reason, sample_rate=8_000, channels=1,
                    sample_width=2, encoding="pcm_s16le", tracks=self.tracks,
                    counters=self.counters)

    def _write(self):
        storage_fd = call_fd = None
        files = {}
        outputs = {}
        failed = False
        try:
            storage_fd = _private_directory(Path(self.settings.media_storage_dir))
            if shutil.disk_usage(self.settings.media_storage_dir).free < MIN_FREE_BYTES:
                self.finish_reason = "low-disk-space"
                self.closing.set()
                return
            os.mkdir(self.ticket.call_sid, mode=0o700, dir_fd=storage_fd)
            call_fd = os.open(self.ticket.call_sid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=storage_fd)
            os.fchmod(call_fd, 0o700)
            for track in TRACKS:
                fd = os.open(f"{track}.wav", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             mode=0o600, dir_fd=call_fd)
                os.fchmod(fd, 0o600)
                files[track] = os.fdopen(fd, "wb")
                output = wave.open(files[track], "wb")
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8_000)
                outputs[track] = output
            disk_checked = time.monotonic()
            while not self.closing.is_set() or not self.frames.empty():
                if time.monotonic() - disk_checked >= 5:
                    disk_checked = time.monotonic()
                    if shutil.disk_usage(self.settings.media_storage_dir).free < MIN_FREE_BYTES:
                        self.finish_reason = "low-disk-space"
                        self.closing.set()
                        break
                try:
                    frame = self.frames.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    if self.finish_reason in {"low-disk-space", "storage-error", "storage-timeout"}:
                        self.counters["dropped_messages"] += 1
                    else:
                        self._write_frame(outputs, frame)
                finally:
                    self.frames.task_done()
        except Exception:
            # Do not log raw frames, tokens, phone numbers, or exception text.
            failed = True
            self.finish_reason = "storage-error"
            self.closing.set()
        finally:
            while True:
                try:
                    self.frames.get_nowait()
                    self.counters["dropped_messages"] += 1
                    self.frames.task_done()
                except queue.Empty:
                    break
            for output in outputs.values():
                try:
                    output.close()
                except Exception:
                    failed = True
                    self.finish_reason = "storage-error"
            for file in files.values():
                try:
                    file.close()
                except Exception:
                    failed = True
                    self.finish_reason = "storage-error"
            clean = (self.finish_reason in {"stream-stopped", "call-ended"}
                     and not self.counters["rejected_messages"]
                     and not self.counters["dropped_messages"])
            status = "failed" if failed else "completed" if clean else "partial"
            manifest = self._manifest(status)
            if call_fd is not None:
                try:
                    fd = os.open(".manifest.tmp", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                 mode=0o600, dir_fd=call_fd)
                    with os.fdopen(fd, "w") as file:
                        json.dump(manifest, file, indent=2)
                        file.write("\n")
                        file.flush()
                        os.fsync(file.fileno())
                    os.replace(".manifest.tmp", "manifest.json", src_dir_fd=call_fd,
                               dst_dir_fd=call_fd)
                except Exception:
                    manifest["status"] = "failed"
                    manifest["finish_reason"] = "storage-error"
            if call_fd is not None:
                os.close(call_fd)
            if storage_fd is not None:
                os.close(storage_fd)
            logger.info("media_capture finished call_sid=%s status=%s reason=%s",
                        self.ticket.call_sid, manifest["status"], manifest["finish_reason"])
            self.done.set_result(manifest)
            try:
                self.loop.call_soon_threadsafe(self.closed_event.set)
            except RuntimeError:
                pass

    async def finish(self, reason: str):
        if not self.closing.is_set():
            self.finish_reason = reason
            self.closing.set()
        # A stalled capture writer must not keep a provider stream alive.
        # The done callback covers independent writer failure; this path covers
        # call end immediately and is deliberately idempotent.
        self._finish_observer()
        websocket, self.websocket = self.websocket, None
        if websocket is not None:
            try:
                await asyncio.wait_for(websocket.close(code=1000), timeout=1)
            except (RuntimeError, OSError, asyncio.TimeoutError):
                pass
        # A stalled filesystem must not block application shutdown forever.
        # The live worker remains counted active, so deployment cannot claim it
        # drained until its actual disk writes and manifest publication finish.
        try:
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self.done)),
                                          timeout=FLUSH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self.finish_reason = "storage-timeout"
            logger.error("media_capture flush_timeout call_sid=%s", self.ticket.call_sid)
            return None


class CaptureManager:
    """Per-process capture registry. Call IDs are never accepted as file paths."""

    def __init__(self, settings, observer=None):
        self.settings = settings
        self.observer = observer
        self.tickets: dict[str, CaptureTicket] = {}
        self.status_tasks: set[asyncio.Task] = set()
        self.closed = False

    def _expire(self):
        now = time.monotonic()
        for sid, ticket in list(self.tickets.items()):
            if not ticket.ended and ticket.session is None and now - ticket.created > TICKET_TTL:
                ticket.ended = True
                ticket.ended_at = now
            if (ticket.ended and now - ticket.ended_at > TOMBSTONE_TTL
                    and (ticket.session is None or ticket.session.done.done())):
                self.tickets.pop(sid, None)

    @property
    def active_count(self) -> int:
        return sum(ticket.session is not None and not ticket.session.done.done()
                   for ticket in self.tickets.values())

    @property
    def pending_count(self) -> int:
        self._expire()
        return sum(not ticket.ended and ticket.session is None for ticket in self.tickets.values())

    def reserve(self, call_sid: str) -> CaptureTicket:
        self._expire()
        if (self.closed or not getattr(self.settings, "media_capture_enabled", False)
                or not SID.fullmatch(call_sid)):
            raise CaptureRejected("Invalid or unavailable capture")
        if call_sid in self.tickets:
            ticket = self.tickets[call_sid]
            if ticket.ended or (ticket.session and ticket.session.done.done()):
                raise CaptureRejected("Capture already ended")
            return ticket
        if len(self.tickets) >= MAX_TICKETS:
            raise CaptureRejected("Capture registry is full")
        ticket = CaptureTicket(secrets.token_urlsafe(32), f"capture-{call_sid}", call_sid)
        self.tickets[call_sid] = ticket
        return ticket

    def validate_start(self, call_sid: str, start: dict) -> CaptureTicket:
        self._expire()
        ticket = self.tickets.get(call_sid)
        if self.closed or ticket is None or ticket.ended or ticket.session is not None:
            raise CaptureRejected("Capture ticket is unavailable")
        try:
            stream_sid = start["streamSid"]
            token = start["customParameters"]["token"]
            fmt = start["mediaFormat"]
            valid = (start["accountSid"] == self.settings.account_sid
                     and start["callSid"] == call_sid
                     and isinstance(stream_sid, str) and STREAM_SID.fullmatch(stream_sid)
                     and (not ticket.stream_sid or ticket.stream_sid == stream_sid)
                     and isinstance(token, str) and hmac.compare_digest(token, ticket.token)
                     and isinstance(start["tracks"], list) and len(start["tracks"]) == 2
                     and set(start["tracks"]) == set(TRACKS)
                     and fmt["encoding"] == "audio/x-mulaw"
                     and fmt["sampleRate"] == 8_000 and fmt["channels"] == 1)
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise CaptureRejected("Twilio start does not match capture ticket")
        ticket.stream_sid = stream_sid
        return ticket

    async def handle(self, websocket, call_sid: str):
        """Consume a signature-verified ASGI WebSocket. Never send media."""
        ticket = self.tickets.get(call_sid)
        if (self.closed or not SID.fullmatch(call_sid) or ticket is None or ticket.ended
                or ticket.session is not None):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        recorder = None
        reason = "socket-disconnected"
        before_start = time.monotonic()
        invalid_messages = 0
        try:
            while True:
                limit = (self.settings.media_max_seconds + 5 if recorder else 5)
                started = recorder.started_monotonic if recorder else before_start
                remaining = limit - (time.monotonic() - started)
                if remaining <= 0:
                    reason = "duration-limit" if recorder else "start-timeout"
                    break
                if recorder and recorder.done.done():
                    reason = recorder.finish_reason
                    break
                try:
                    if recorder:
                        receive = asyncio.create_task(websocket.receive())
                        closed = asyncio.create_task(recorder.closed_event.wait())
                        try:
                            ready, _ = await asyncio.wait((receive, closed), timeout=remaining,
                                                         return_when=asyncio.FIRST_COMPLETED)
                            if closed in ready:
                                reason = recorder.finish_reason
                                break
                            if receive not in ready:
                                raise asyncio.TimeoutError
                            message = receive.result()
                        finally:
                            for task in (receive, closed):
                                if not task.done():
                                    task.cancel()
                            await asyncio.gather(receive, closed, return_exceptions=True)
                    else:
                        message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    reason = "duration-limit" if recorder else "start-timeout"
                    break
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text", "")
                if (not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES
                        or not raw):
                    reason = "invalid-message"
                    break
                try:
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise ValueError("Expected an object")
                except (ValueError, TypeError):
                    invalid_messages += 1
                    if recorder:
                        recorder.counters["rejected_messages"] += 1
                    if invalid_messages >= 10:
                        reason = "invalid-message"
                        break
                    continue
                kind = event.get("event")
                if kind == "connected" and recorder is None:
                    invalid_messages += 1
                    if invalid_messages > 5:
                        break
                    continue
                if kind == "start" and recorder is None:
                    start = event.get("start", {})
                    if not isinstance(start, dict) or event.get("streamSid") != start.get("streamSid"):
                        raise CaptureRejected("Start stream identifier mismatch")
                    ticket = self.validate_start(call_sid, start)
                    recorder = _Recorder(self.settings, ticket, websocket, self.observer)
                    ticket.session = recorder
                    _observe(self.observer, "start", call_sid, ticket.stream_sid)
                    continue
                if recorder is None:
                    raise CaptureRejected("Expected Twilio start")
                if kind == "media":
                    stop_reason = recorder.media(event)
                    if stop_reason:
                        reason = stop_reason
                        break
                elif kind == "stop":
                    stop = event.get("stop", {})
                    if not isinstance(stop, dict):
                        recorder.counters["rejected_messages"] += 1
                        continue
                    if (event.get("streamSid") == ticket.stream_sid
                            and stop.get("accountSid") == self.settings.account_sid
                            and stop.get("callSid") == call_sid):
                        reason = "stream-stopped"
                        break
                    recorder.counters["rejected_messages"] += 1
                else:
                    recorder.counters["rejected_messages"] += 1
        except CaptureRejected:
            reason = "invalid-start"
        except (RuntimeError, OSError):
            reason = "socket-disconnected"
        finally:
            if recorder:
                await recorder.finish(reason)
                ticket.ended = True
                ticket.ended_at = time.monotonic()
            else:
                try:
                    await websocket.close(code=1008)
                except (RuntimeError, OSError):
                    pass

    def mark_status(self, call_sid: str, stream_sid: str, status: str, *,
                    stream_name: str = "", error: str | None = None) -> bool:
        """Consume an already signature-verified Twilio stream status callback."""
        ticket = self.tickets.get(call_sid)
        if (ticket is None or ticket.ended or not isinstance(stream_sid, str)
                or not STREAM_SID.fullmatch(stream_sid) or stream_name != ticket.stream_name
                or (ticket.stream_sid and ticket.stream_sid != stream_sid)
                or status not in {"stream-started", "stream-stopped", "stream-error"}):
            return False
        ticket.stream_sid = stream_sid
        if status in {"stream-stopped", "stream-error"}:
            reason = "stream-stopped" if status == "stream-stopped" else "stream-error"
            task = asyncio.create_task(self.finish(call_sid, reason=reason))
            self.status_tasks.add(task)
            task.add_done_callback(self.status_tasks.discard)
        return True

    async def finish(self, call_sid: str, reason: str = "call-ended"):
        if not SID.fullmatch(call_sid):
            return
        ticket = self.tickets.get(call_sid)
        if ticket is None:
            self._expire()
            if len(self.tickets) >= MAX_TICKETS:
                return
            ticket = CaptureTicket("", f"capture-{call_sid}", call_sid)
            self.tickets[call_sid] = ticket
        ticket.ended = True
        ticket.ended_at = time.monotonic()
        if ticket.session and not ticket.session.done.done():
            # Webhook delivery can beat the final media/stop event.
            await asyncio.sleep(1.0)
            await ticket.session.finish(reason)

    async def close(self):
        self.closed = True
        await asyncio.gather(*(self.finish(sid, reason="server-shutdown")
                               for sid in list(self.tickets)))
        if self.status_tasks:
            await asyncio.gather(*list(self.status_tasks), return_exceptions=True)
