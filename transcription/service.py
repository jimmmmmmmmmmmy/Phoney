"""Two bounded Deepgram streams fed by validated Twilio caller-leg audio.

Protocol: https://developers.deepgram.com/reference/speech-to-text/listen-streaming
KeepAlive and CloseStream are JSON text; audio is raw 8000 Hz mono mu-law.
Observer methods are synchronous, nonblocking and never change call routing.
"""

from __future__ import annotations

import asyncio
from collections import deque
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import re
from urllib.parse import urlencode

import websockets

from . import storage

TRACKS = ("inbound", "outbound")
MEANINGS = {"inbound": "caller-input", "outbound": "caller-playback"}
MAX_ACTIVE = 2
QUEUE_FRAMES = 128
QUEUE_BYTES = 64_000
MAX_SEGMENTS = 2000
MAX_TEXT = 2000
MAX_SESSION_TEXT = 100_000
MAX_MESSAGE = 65_536
KEEPALIVE_SECONDS = 3.0
CONNECT_SECONDS = 5.0
FLUSH_SECONDS = 10.0
SEND_SECONDS = 3.0
STORE_SECONDS = 5.0
SILENCE = b"\xff" * 4000
SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
STREAM_SID = re.compile(r"MZ[0-9a-fA-F]{32}\Z")
ERRORS = {"provider-unavailable", "provider-error", "provider-disconnected", "result-invalid",
          "audio-queue-overflow", "audio-order", "invalid-audio", "duration-limit",
          "transcript-limit", "flush-timeout", "send-timeout", "server-shutdown", "capacity-limit"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def speech_start_ms(alternative, start, end):
    """Return a bounded speech onset, not the result window's leading silence."""
    words = alternative.get("words")
    if not isinstance(words, list) or not words or not isinstance(words[0], dict):
        return None
    first = words[0]
    word = first.get("word")
    word_start, word_end = first.get("start"), first.get("end")
    if (not isinstance(word, str) or not word.strip()
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   for value in (word_start, word_end))
            or not start <= word_start <= word_end <= end):
        return None
    return round(word_start * 1000)


class TrackFailure(Exception):
    pass


@dataclass
class _Track:
    name: str
    status: str = "connecting"
    error: str = ""
    interim: str = ""
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_FRAMES))
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    queued_bytes: int = 0
    offered_samples: int = 0
    sent_samples: int = 0
    finishing: bool = False
    close_sent: bool = False
    task: asyncio.Task | None = None


@dataclass
class _Session:
    call_sid: str
    stream_sid: str
    started_at: str = field(default_factory=now)
    ended_at: str | None = None
    status: str = "connecting"
    finish_reason: str = ""
    storage_error: str = ""
    tracks: dict = field(default_factory=lambda: {name: _Track(name) for name in TRACKS})
    segments: list = field(default_factory=list)
    final_keys: dict = field(default_factory=dict)
    text_chars: int = 0
    finishing: bool = False
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class TranscriptionManager:
    def __init__(self, settings, connector=None, on_segment=None, on_failure=None):
        self.settings = settings
        self.enabled = bool(getattr(settings, "transcription_enabled", False))
        self.model = getattr(settings, "deepgram_model", "nova-3")
        self.connector = connector or websockets.connect
        self.on_segment = on_segment
        self.on_failure = on_failure
        self.sessions: dict[str, _Session] = {}
        self.tasks: set[asyncio.Task] = set()
        self.storage_tasks: set[asyncio.Task] = set()
        self.history: list[dict] = []
        self.seen: deque[str] = deque(maxlen=1000)
        self.revision = 0
        self.closed = False
        self.storage_error = ""
        self.archive = storage.ArchiveIndex(getattr(settings, "transcript_storage_dir", ""),
                                            self._valid_history)
        if self.enabled:
            try:
                self.history = [item for item in storage.load(settings.transcript_storage_dir)
                                if self._valid_history(item)]
                self.seen.extend(item["call_sid"] for item in self.history)
                if self.history:
                    self.revision += 1
            except (OSError, ValueError, TypeError):
                self.storage_error = "storage-unavailable"

    @property
    def active_count(self) -> int:
        return sum(not task.done() for task in self.tasks | self.storage_tasks)

    def _touch(self):
        self.revision += 1

    def archive_snapshot(self):
        return self.archive.snapshot()

    def get_saved_call(self, call_sid):
        """Disk-only lookup; dashboard keeps active/in-memory calls authoritative."""
        try:
            document = storage.load_call(self.settings.transcript_storage_dir, call_sid)
            return document if document and self._valid_history(document) else None
        except (OSError, ValueError, TypeError):
            return None

    def _notify_segment(self, call_sid, segment, final, *, speech_start_ms=None):
        # Observers enqueue bounded work; provider/control I/O never runs here.
        if self.on_segment is not None:
            try:
                event = dict(segment)
                if speech_start_ms is not None:
                    # Control-only metadata must not alter stored timing or IDs.
                    event["speech_start_ms"] = speech_start_ms
                self.on_segment(call_sid, event, final)
            except Exception:
                pass

    def call_segments(self, call_sid):
        session = self.sessions.get(call_sid)
        if session is not None:
            segments = session.segments
        else:
            document = next((item for item in self.history if item["call_sid"] == call_sid), None)
            segments = document["segments"] if document else []
        return copy.deepcopy(sorted(segments, key=lambda item: (item["start_ms"], item["id"])))

    def add_agent_segment(self, call_sid, text, start_ms, end_ms, *, name="Agent", delivery="played"):
        """Record delivered agent text, never re-transcribe our synthesized voice."""
        session = self.sessions.get(call_sid)
        if (session is None or session.finishing or not isinstance(text, str)
                or not text.strip() or len(text) > MAX_TEXT
                or type(start_ms) is not int or type(end_ms) is not int
                or not 0 <= start_ms <= end_ms <= self.settings.media_max_seconds * 1000
                or delivery not in {"played", "interrupted"}
                or not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
                or any(ord(c) < 32 for c in name)):
            return False
        if (len(session.segments) >= MAX_SEGMENTS
                or session.text_chars + len(text) > MAX_SESSION_TEXT):
            return False
        segment = {"id": f"agent-{len(session.segments)}", "track": "outbound",
                   "start_ms": start_ms, "end_ms": end_ms, "text": text.strip(),
                   "confidence": 1.0, "source": "agent", "speaker": name.strip(),
                   "delivery": delivery}
        session.segments.append(segment)
        session.text_chars += len(segment["text"])
        self._touch()
        return True

    def start(self, call_sid: str, stream_sid: str) -> None:
        if (not self.enabled or self.closed or call_sid in self.seen or call_sid in self.sessions
                or not isinstance(call_sid, str) or not SID.fullmatch(call_sid)
                or not isinstance(stream_sid, str) or not STREAM_SID.fullmatch(stream_sid)):
            return
        if len(self.sessions) >= MAX_ACTIVE:
            rejected = _Session(call_sid, stream_sid)
            rejected.status = "failed"
            rejected.ended_at = now()
            rejected.finish_reason = "capacity-limit"
            rejected.storage_error = "not-recorded"
            for track in rejected.tracks.values():
                track.status = "failed"
                track.error = "capacity-limit"
            self.history.insert(0, self._document(rejected))
            self.history = self.history[:storage.MAX_HISTORY]
            self.seen.append(call_sid)
            self._touch()
            return
        session = _Session(call_sid, stream_sid)
        self.sessions[call_sid] = session
        self.seen.append(call_sid)
        session.task = asyncio.create_task(self._run_session(session), name="live-transcription")
        self.tasks.add(session.task)
        session.task.add_done_callback(self.tasks.discard)
        self._touch()

    def _fail(self, session: _Session, track: _Track, error: str):
        if not track.error:
            track.error = error if error in ERRORS else "provider-error"
            track.status = "failed"
            track.interim = ""
            track.finishing = True
            track.stop.set()
            track.wake.set()
            self._touch()
            callback = self.on_failure
            if callback is not None:
                try:
                    callback(session.call_sid, track.name)
                except Exception:
                    pass

    def offer(self, call_sid: str, track: str, timestamp_ms: int, payload: bytes) -> None:
        session = self.sessions.get(call_sid)
        if not session or session.finishing or track not in TRACKS:
            return
        state = session.tracks[track]
        if state.error or state.finishing:
            return
        error = ""
        if (type(timestamp_ms) is not int or timestamp_ms < 0 or not isinstance(payload, bytes)
                or not payload or len(payload) > 8000):
            error = "invalid-audio"
        elif timestamp_ms * 8 < state.offered_samples:
            error = "audio-order"
        elif timestamp_ms * 8 + len(payload) > self.settings.media_max_seconds * 8000:
            error = "duration-limit"
        elif state.queue.full() or state.queued_bytes + len(payload) > QUEUE_BYTES:
            error = "audio-queue-overflow"
        if error:
            self._fail(session, state, error)
            if state.task:
                state.task.cancel()
            return
        state.queue.put_nowait((timestamp_ms, payload))
        state.queued_bytes += len(payload)
        state.offered_samples = timestamp_ms * 8 + len(payload)
        state.wake.set()

    def finish(self, call_sid: str, reason: str = "call-ended") -> None:
        session = self.sessions.get(call_sid)
        if not session or session.finishing:
            return
        session.finishing = True
        session.status = "finishing"
        session.finish_reason = reason if reason in {
            "call-ended", "stream-stopped", "socket-disconnected", "duration-limit",
            "server-shutdown", "storage-error", "storage-timeout", "low-disk-space",
            "queue-overflow", "invalid-message", "stream-error"} else "capture-ended"
        for track in session.tracks.values():
            track.finishing = True
            track.stop.set()
            if not track.error:
                track.status = "finishing"
            track.wake.set()
        self._touch()

    def _document(self, session: _Session) -> dict:
        return {"schema_version": 1, "provider": "deepgram", "model": self.model,
                "call_sid": session.call_sid, "stream_sid": session.stream_sid,
                "started_at": session.started_at, "ended_at": session.ended_at,
                "status": session.status, "finish_reason": session.finish_reason,
                "storage_error": session.storage_error,
                "tracks": {name: {"meaning": MEANINGS[name], "status": track.status,
                                  "error": track.error, "interim": track.interim}
                           for name, track in session.tracks.items()},
                "segments": sorted(session.segments, key=lambda item: (item["start_ms"], item["id"]))}

    def snapshot(self) -> dict:
        live = [self._document(session) for session in reversed(list(self.sessions.values()))]
        return copy.deepcopy({"enabled": self.enabled, "provider": "deepgram", "model": self.model,
                              "revision": self.revision, "storage_error": self.storage_error,
                              "sessions": live + self.history[:storage.MAX_HISTORY]})

    async def _run_session(self, session: _Session):
        async def duration_guard():
            await asyncio.sleep(self.settings.media_max_seconds)
            self.finish(session.call_sid, "duration-limit")

        guard = asyncio.create_task(duration_guard())
        try:
            for track in session.tracks.values():
                track.task = asyncio.create_task(self._run_track(session, track),
                                                 name=f"transcription-{track.name}")
            await asyncio.gather(*(track.task for track in session.tracks.values()),
                                 return_exceptions=True)
        except asyncio.CancelledError:
            for track in session.tracks.values():
                self._fail(session, track, "server-shutdown")
                if track.task:
                    track.task.cancel()
            await asyncio.gather(*(track.task for track in session.tracks.values() if track.task),
                                 return_exceptions=True)
        finally:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)
            failed = sum(bool(track.error) for track in session.tracks.values())
            session.status = "failed" if failed == 2 else "partial" if failed else "completed"
            if session.finish_reason not in {"call-ended", "stream-stopped"} and not failed:
                session.status = "partial"
            session.ended_at = now()
            for track in session.tracks.values():
                track.interim = ""
            document = self._document(session)
            task = asyncio.create_task(asyncio.to_thread(storage.save,
                                                        self.settings.transcript_storage_dir, document))
            self.storage_tasks.add(task)
            task.add_done_callback(self._storage_done)
            try:
                await asyncio.wait_for(asyncio.shield(task), STORE_SECONDS)
            except (Exception, asyncio.CancelledError):
                session.storage_error = "storage-failed"
                document["storage_error"] = session.storage_error
            self.history.insert(0, document)
            self.history = self.history[:storage.MAX_HISTORY]
            self.sessions.pop(session.call_sid, None)
            session.finished.set()
            self._touch()

    def _storage_done(self, task):
        self.storage_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve failures without logging private content.

    async def _run_track(self, session: _Session, track: _Track):
        children = []
        try:
            if track.error:
                return
            params = {"model": self.model, "encoding": "mulaw", "sample_rate": 8000,
                      "channels": 1, "interim_results": "true", "smart_format": "true",
                      "endpointing": 300}
            url = "wss://api.deepgram.com/v1/listen?" + urlencode(params)
            async with self.connector(url, additional_headers={
                    "Authorization": "Token " + self.settings.deepgram_api_key},
                    open_timeout=CONNECT_SECONDS, close_timeout=1,
                    max_size=MAX_MESSAGE, max_queue=16) as socket:
                if track.error:
                    return
                track.status = "finishing" if track.finishing else "live"
                if not session.finishing:
                    session.status = "live"
                self._touch()
                sender = asyncio.create_task(self._send(socket, session, track))
                receiver = asyncio.create_task(self._receive(socket, session, track))
                async def flush_guard():
                    await track.stop.wait()
                    await asyncio.sleep(FLUSH_SECONDS)
                    raise TrackFailure("flush-timeout")

                deadline = asyncio.create_task(flush_guard())
                children = [sender, receiver, deadline]
                done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
                if deadline in done:
                    await deadline
                if receiver in done:
                    await receiver
                    if not track.close_sent:
                        raise TrackFailure("provider-disconnected")
                await sender
                done, _ = await asyncio.wait([receiver, deadline], return_when=asyncio.FIRST_COMPLETED)
                if deadline in done:
                    await deadline
                await receiver
                if not track.error:
                    track.status = "completed"
                    self._touch()
        except TrackFailure as exc:
            self._fail(session, track, str(exc))
        except asyncio.CancelledError:
            if not track.error:
                self._fail(session, track, "server-shutdown")
        except Exception:
            self._fail(session, track, "provider-unavailable")
        finally:
            for task in children:
                if not task.done():
                    task.cancel()
            if children:
                await asyncio.gather(*children, return_exceptions=True)
            track.interim = ""
            while not track.queue.empty():
                track.queue.get_nowait()
            track.queued_bytes = 0

    async def _send(self, socket, session: _Session, track: _Track):
        async def send(data):
            try:
                await asyncio.wait_for(socket.send(data), SEND_SECONDS)
            except asyncio.TimeoutError:
                raise TrackFailure("send-timeout")

        while True:
            if track.error:
                return
            try:
                timestamp, payload = track.queue.get_nowait()
            except asyncio.QueueEmpty:
                if track.finishing:
                    track.close_sent = True
                    await send(json.dumps({"type": "CloseStream"}))
                    return
                track.wake.clear()
                try:
                    await asyncio.wait_for(track.wake.wait(), KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    await send(json.dumps({"type": "KeepAlive"}))
                continue
            track.queued_bytes -= len(payload)
            padding = timestamp * 8 - track.sent_samples
            if padding < 0:
                raise TrackFailure("audio-order")
            while padding:
                count = min(padding, len(SILENCE))
                await send(SILENCE[:count])
                padding -= count
                track.sent_samples += count
            await send(payload)
            track.sent_samples += len(payload)

    async def _receive(self, socket, session: _Session, track: _Track):
        async for raw in socket:
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE:
                raise TrackFailure("result-invalid")
            try:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError
                kind = event.get("type")
                if kind == "Error" or "err_code" in event:
                    raise TrackFailure("provider-error")
                if kind in {"Metadata", "UtteranceEnd", "SpeechStarted"}:
                    continue
                if kind != "Results":
                    raise ValueError
                alternative = event["channel"]["alternatives"][0]
                text = alternative["transcript"]
                final = event["is_final"]
                start, duration = event["start"], event["duration"]
                confidence = alternative.get("confidence", 0.0)
                if (not isinstance(text, str) or len(text) > MAX_TEXT or type(final) is not bool
                        or any(type(value) not in (int, float) or not math.isfinite(value)
                               for value in (start, duration, confidence))
                        or start < 0 or duration < 0 or not 0 <= confidence <= 1):
                    raise ValueError
                start_ms, end_ms = round(start * 1000), round((start + duration) * 1000)
                if end_ms > min(self.settings.media_max_seconds * 1000,
                                math.ceil(track.offered_samples / 8) + 50):
                    raise ValueError
            except (ValueError, KeyError, TypeError, IndexError):
                raise TrackFailure("result-invalid")
            text = text.strip()
            onset_ms = speech_start_ms(alternative, start, start + duration)
            if final:
                if track.interim:
                    track.interim = ""
                    self._touch()
                if text:
                    key = (track.name, start_ms, end_ms)
                    existing = session.final_keys.get(key)
                    old_text = session.segments[existing]["text"] if existing is not None else ""
                    total = session.text_chars - len(old_text) + len(text)
                    if (total > MAX_SESSION_TEXT or
                            (existing is None and len(session.segments) >= MAX_SEGMENTS)):
                        raise TrackFailure("transcript-limit")
                    segment = {"id": f"{track.name}-{existing if existing is not None else len(session.segments)}",
                               "track": track.name, "start_ms": start_ms, "end_ms": end_ms,
                               "text": text, "confidence": float(confidence)}
                    if existing is None:
                        session.final_keys[key] = len(session.segments)
                        session.segments.append(segment)
                    elif session.segments[existing] == segment:
                        continue
                    else:
                        session.segments[existing] = segment
                    session.text_chars = total
                    self._touch()
                    self._notify_segment(session.call_sid, segment, True,
                                         speech_start_ms=onset_ms)
            elif track.interim != text:
                track.interim = text
                self._touch()
                self._notify_segment(session.call_sid, {"track": track.name,
                    "text": text, "start_ms": start_ms, "end_ms": end_ms}, False,
                    speech_start_ms=onset_ms)
        if not track.close_sent:
            raise TrackFailure("provider-disconnected")

    async def close(self):
        self.closed = True
        for sid in list(self.sessions):
            self.finish(sid, "server-shutdown")
        tasks = list(self.tasks)
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=CONNECT_SECONDS + FLUSH_SECONDS + 2)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self.storage_tasks:
            await asyncio.wait(list(self.storage_tasks), timeout=STORE_SECONDS)

    def _valid_history(self, document: dict) -> bool:
        try:
            if (document.get("schema_version") != 1 or document.get("provider") != "deepgram"
                    or not SID.fullmatch(document["call_sid"])
                    or not STREAM_SID.fullmatch(document["stream_sid"])
                    or document["status"] not in {"completed", "partial", "failed"}
                    or not isinstance(document["model"], str) or len(document["model"]) > 100
                    or not isinstance(document["finish_reason"], str) or len(document["finish_reason"]) > 100
                    or document["storage_error"] not in {"", "storage-failed"}):
                return False
            for name in ("started_at", "ended_at"):
                if len(document[name]) > 40:
                    return False
                datetime.fromisoformat(document[name])
            for name in TRACKS:
                track = document["tracks"][name]
                if (track["meaning"] != MEANINGS[name] or track["status"] not in {"completed", "failed"}
                        or track["error"] not in ERRORS | {""} or track["interim"] != ""):
                    return False
            segments = document["segments"]
            if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
                return False
            chars = 0
            for segment in segments:
                if (segment["track"] not in TRACKS or not isinstance(segment["text"], str)
                        or len(segment["text"]) > MAX_TEXT or not isinstance(segment["id"], str)
                        or len(segment["id"]) > 40 or type(segment["start_ms"]) is not int
                        or type(segment["end_ms"]) is not int
                        or not 0 <= segment["start_ms"] <= segment["end_ms"] <= 3_600_000
                        or type(segment["confidence"]) not in (int, float)
                        or not math.isfinite(segment["confidence"])
                        or not 0 <= segment["confidence"] <= 1):
                    return False
                if segment.get("source") == "agent":
                    if (segment["track"] != "outbound"
                            or not isinstance(segment.get("speaker"), str)
                            or not 1 <= len(segment["speaker"]) <= 80
                            or any(ord(c) < 32 for c in segment["speaker"])
                            or segment.get("delivery") not in {"played", "interrupted"}):
                        return False
                chars += len(segment["text"])
            return chars <= MAX_SESSION_TEXT
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            return False
