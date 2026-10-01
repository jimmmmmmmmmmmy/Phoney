"""Bounded private voice diagnostics; media callbacks only append in memory."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import stat
import wave
from media_capture.capture import decode_mulaw
from transcription.storage import private_directory

CALL_SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
MAX_AUDIO_BYTES = 8000 * 180
MAX_EVENTS = 1200
MAX_ACTIVE = 16
MAX_JSON_BYTES = 1024 * 1024
MAX_WAV_BYTES = MAX_AUDIO_BYTES * 2 + 44
DECODE_CHUNK_BYTES = 32000
log = logging.getLogger("uvicorn.error")


class CallQualityRecorder:
    def __init__(self, settings):
        self.path = (Path(settings.call_details_storage_dir) / "audio-quality"
                     if settings.call_details_storage_dir else None)
        self.audio_enabled = bool(settings.media_capture_enabled)
        self.calls = {}
        self.writes = set()

    @property
    def pending_count(self):
        return len(self.writes)

    def start(self, call_sid):
        if (self.path is None or not CALL_SID.fullmatch(call_sid or "")
                or call_sid in self.calls or len(self.calls) + len(self.writes) >= MAX_ACTIVE):
            return
        self.calls[call_sid] = {"events": deque(maxlen=MAX_EVENTS),
            "generated": bytearray(), "sent": bytearray(), "total_generated": 0,
            "total_sent": 0, "started_at": datetime.now(timezone.utc).isoformat(),
            "events_total": 0, "last_sent_kind": None}

    def event(self, call_sid, event, elapsed_ms, **fields):
        call = self.calls.get(call_sid)
        if call is None:
            return
        row = {"event": str(event)[:100], "elapsed_ms": max(0, int(elapsed_ms))}
        # Keep scalar timings/control states. Never persist arbitrary text,
        # HTTP bodies, credentials, prompts, or full provider payloads.
        for key, value in fields.items():
            if any(word in key.lower() for word in ("text", "token", "secret", "key", "url", "prompt")):
                continue
            if type(value) in (int, bool) or value is None:
                row[key[:80]] = value
            elif type(value) is float and math.isfinite(value):
                row[key[:80]] = value
            elif isinstance(value, str) and len(value) <= 100 and re.fullmatch(r"[A-Za-z0-9_.:/ -]*", value):
                row[key[:80]] = value
        call["events"].append(row)
        call["events_total"] += 1

    def audio(self, call_sid, track, chunk, elapsed_ms, *, kind="agent", phase=""):
        call = self.calls.get(call_sid)
        if call is None or track not in {"generated", "sent"} or not isinstance(chunk, bytes):
            return
        offset = call["total_" + track]
        call["total_" + track] += len(chunk)
        if self.audio_enabled and len(call[track]) < MAX_AUDIO_BYTES:
            call[track].extend(chunk[:MAX_AUDIO_BYTES - len(call[track])])
        if track == "generated" or call["last_sent_kind"] != kind:
            self.event(call_sid, "audio-source" if track == "generated" else "audio-send-kind",
                elapsed_ms, track=track, kind=kind, phase=phase,
                offset_bytes=offset, bytes=len(chunk))
        if track == "sent":
            call["last_sent_kind"] = kind

    def snapshot_live(self, call_sid):
        """Copy live observations on the event loop that owns their mutation."""
        call = self.calls.get(call_sid)
        return self._document(call_sid, call, completed=False) if call is not None else None

    def snapshot(self, call_sid):
        """Read a completed record with bounded, symlink-resistant file access."""
        try:
            data = self._read(call_sid, "diagnostics.json", MAX_JSON_BYTES)
            if data is None:
                return None
            document = json.loads(data)
            return document if document.get("call_sid") == call_sid else None
        except (OSError, ValueError, AttributeError, RecursionError):
            return None

    def _document(self, sid, call, *, completed):
        return {"schema_version": 1, "call_sid": sid, "started_at": call["started_at"],
            "completed": completed, "sample_rate": 8000, "encoding": "mulaw",
            "audio_timeline": "concatenated provider/source bytes; use event offsets, not call-time playback",
            "audio_capture_enabled": self.audio_enabled,
            "audio": {track: {"total_bytes": call["total_" + track],
                "saved_bytes": len(call[track]), "truncated": len(call[track]) < call["total_" + track]}
                for track in ("generated", "sent")},
            "events_total": call["events_total"], "events": list(call["events"])}

    async def finish(self, call_sid):
        call = self.calls.pop(call_sid, None)
        if call is None or self.path is None:
            return
        document = self._document(call_sid, call, completed=True)
        task = asyncio.create_task(asyncio.to_thread(self._save, call_sid, call, document))
        self.writes.add(task)
        task.add_done_callback(self._write_done)

    def _write_done(self, task):
        self.writes.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.warning("call_quality_save_unavailable")

    def _directory(self, sid, *, create=False):
        if (self.path is None or not isinstance(sid, str) or not CALL_SID.fullmatch(sid)
                or (not create and not self.path.exists())):
            raise FileNotFoundError("Quality record is unavailable")
        # Set both the diagnostic root and the per-call directory private.
        root = private_directory(self.path)
        try:
            if create:
                try:
                    os.mkdir(sid, mode=0o700, dir_fd=root)
                except FileExistsError:
                    pass
            child = os.open(sid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            os.fchmod(child, 0o700)
            return child
        finally:
            os.close(root)

    def _read(self, sid, name, limit):
        root = self._directory(sid)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    return None
                data = source.read(limit + 1)
            return data if len(data) <= limit else None
        finally:
            os.close(root)

    def _save(self, sid, call, document):
        root = self._directory(sid, create=True)
        try:
            for track in ("generated", "sent"):
                if call[track]:
                    self._save_file(root, track + ".wav", call[track], audio=True)
            payload = json.dumps(document, separators=(",", ":"), allow_nan=False).encode()
            if len(payload) > MAX_JSON_BYTES:
                raise ValueError("Quality record exceeds storage bound")
            self._save_file(root, "diagnostics.json", payload)
        finally:
            os.close(root)

    def _save_file(self, root, name, data, *, audio=False):
        temporary = f".{name}.{secrets.token_hex(6)}.tmp"
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         mode=0o600, dir_fd=root)
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                if audio:
                    with wave.open(output, "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(8000)
                        # Whole-track decode builds >100MB of temporary join
                        # buffers. Small blocks keep simultaneous saves bounded.
                        for offset in range(0, len(data), DECODE_CHUNK_BYTES):
                            wav.writeframesraw(decode_mulaw(bytes(data[offset:offset + DECODE_CHUNK_BYTES])))
                else:
                    output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, name, src_dir_fd=root, dst_dir_fd=root)
        finally:
            try:
                os.unlink(temporary, dir_fd=root)
            except FileNotFoundError:
                pass

    def read_audio(self, sid, track):
        """Read authorized WAV bytes safely; never return a followable path."""
        if track not in {"generated", "sent"}:
            return None
        try:
            return self._read(sid, track + ".wav", MAX_WAV_BYTES)
        except (OSError, ValueError):
            return None

    async def close(self):
        for sid in list(self.calls):
            await self.finish(sid)
        if self.writes:
            await asyncio.gather(*list(self.writes), return_exceptions=True)
