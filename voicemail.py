"""Small durable voicemail receipts, independent from audio/STT providers.

Only metadata is retained here. Twilio recording URLs, phone numbers, and
credentials never enter the public receipt. Existing capture owns local WAVs.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import threading

from transcription import storage

SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
RECORDING_SID = re.compile(r"RE[0-9a-fA-F]{32}\Z")
STATUSES = {"awaiting", "recording", "processing", "completed", "absent", "failed"}
TERMINAL = {"completed", "absent", "failed"}
MAX_RECEIPT_BYTES = 8192
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def now():
    return datetime.now(timezone.utc).isoformat()


def _open_root(path):
    path = Path(path)
    if not path.is_absolute() or path == Path(path.anchor) or ".." in path.parts:
        raise ValueError("Invalid voicemail storage root")
    root = os.open(path.anchor, DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=root)
            os.close(root)
            root = child
        return root
    except BaseException:
        os.close(root)
        raise


class VoicemailStore:
    """Bounded in-memory receipts and a serial, coalescing disk writer."""

    def __init__(self, settings):
        self.enabled = settings.voicemail_enabled or getattr(settings, "voicemail_agent_enabled", False)
        self.path = settings.voicemail_storage_dir
        if not self.path and getattr(settings, "voicemail_agent_enabled", False):
            # The operator already requires durable transcript storage. Keep its
            # receipt directory beside it when the legacy option is unset.
            self.path = str(Path(settings.transcript_storage_dir).parent / "voicemails")
        self.max_seconds = settings.voicemail_max_seconds
        self.records = {}
        self.storage_error = ""
        self._dirty = {}
        self._worker = None
        self._loads = set()
        self._writing = set()
        self._lock = threading.RLock()
        if self.enabled:
            try:
                for item in storage.load(self.path):
                    record = self._validated(item)
                    if record:
                        self.records[record["call_sid"]] = record
            except (OSError, ValueError):
                self.storage_error = "load-failed"

    @staticmethod
    def _validated(item):
        keys = {"schema_version", "call_sid", "mode", "reason", "started_at", "ended_at",
                "recording_status", "recording_sid", "duration_seconds", "storage_error"}
        if (not isinstance(item, dict) or set(item) != keys or item.get("schema_version") != 1
                or not isinstance(item.get("mode"), str)
                or item["mode"] not in {"voicemail_stub", "voicemail_ai", "voicemail_fallback"}
                or not isinstance(item.get("call_sid"), str)
                or not SID.fullmatch(item["call_sid"])
                or not isinstance(item.get("recording_status"), str)
                or item["recording_status"] not in STATUSES):
            return None
        if any(not isinstance(item[k], str) or len(item[k]) > 100
               for k in ("reason", "started_at", "recording_sid", "storage_error")):
            return None
        if item["recording_sid"] and not RECORDING_SID.fullmatch(item["recording_sid"]):
            return None
        if item["ended_at"] is not None and (not isinstance(item["ended_at"], str)
                                            or len(item["ended_at"]) > 100):
            return None
        if item["duration_seconds"] is not None and (
                type(item["duration_seconds"]) is not int or not 0 <= item["duration_seconds"] <= (86400 if item["mode"] == "voicemail_ai" else 605)):
            return None
        if item["storage_error"] not in {"", "save-failed"}:
            return None
        return deepcopy(item)

    @property
    def active_count(self):
        return int(self._worker is not None and not self._worker.done()) + sum(not t.done() for t in self._loads)

    def _read_one(self, call_sid, root_fd=None):
        """Late signed callbacks can refer to receipts outside the ten-row view."""
        if not isinstance(call_sid, str) or not SID.fullmatch(call_sid):
            return None
        own_root = root_fd is None
        root = _open_root(self.path) if own_root else root_fd
        try:
            fd = os.open(call_sid + ".json", FILE_FLAGS, dir_fd=root)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_RECEIPT_BYTES:
                    return None
                data = os.pread(fd, MAX_RECEIPT_BYTES + 1, 0)
                if len(data) != info.st_size:
                    return None
            finally:
                os.close(fd)
            item = json.loads(data)
            if not isinstance(item, dict) or item.get("call_sid") != call_sid:
                return None
            return self._validated(item)
        finally:
            if own_root:
                os.close(root)

    def _owned_records(self):
        """Pending callbacks/writes remain authoritative while disk catches up."""
        with self._lock:
            return {sid: deepcopy(record) for sid, record in self.records.items()
                    if (record["ended_at"] is None or sid in self._dirty
                        or sid in self._writing or record["storage_error"] == "save-failed")}

    def get(self, call_sid):
        """Read one receipt independently of the recent ten-item view."""
        if not self.enabled or not isinstance(call_sid, str) or not SID.fullmatch(call_sid):
            return None
        record = None
        try:
            record = self._read_one(call_sid)
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            pass
        return self._owned_records().get(call_sid, record)

    def archive_snapshot(self):
        """All validated receipt metadata, with no recent-history scan cutoff."""
        result = {"enabled": self.enabled, "storage_error": self.storage_error, "voicemails": []}
        if not self.enabled:
            return result
        records = {}
        try:
            root = _open_root(self.path)
            try:
                with os.scandir(root) as entries:
                    for entry in entries:
                        if not entry.name.endswith(".json") or not SID.fullmatch(entry.name[:-5]):
                            continue
                        try:
                            record = self._read_one(entry.name[:-5], root)
                        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                            record = None
                        if record is None:
                            result["storage_error"] = result["storage_error"] or "load-failed"
                        else:
                            records[record["call_sid"]] = record
            finally:
                os.close(root)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            result["storage_error"] = result["storage_error"] or "load-failed"
        records.update(self._owned_records())
        if any(record["storage_error"] for record in records.values()):
            result["storage_error"] = result["storage_error"] or "save-failed"
        result["voicemails"] = sorted(records.values(), key=lambda r: (
            r["ended_at"] is None, r["started_at"], r["call_sid"]), reverse=True)
        return result

    async def restore(self, call_sid):
        if call_sid in self.records or not self.path or not SID.fullmatch(call_sid):
            return
        if len(self._loads) >= 16:
            return
        task = asyncio.create_task(asyncio.to_thread(self._read_one, call_sid))
        self._loads.add(task)
        # Read completion stays visible to deployment even if HTTP wait expires.
        def done(task):
            self._loads.discard(task)
            if not task.cancelled():
                task.exception()
        task.add_done_callback(done)
        try:
            record = await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (OSError, ValueError, TimeoutError):
            return
        with self._lock:
            if not record or call_sid in self.records:
                return
            if len(self.records) >= 64:
                finished = [r for r in self.records.values() if r["ended_at"]]
                if not finished:
                    return
                oldest = min(finished, key=lambda r: r["started_at"])
                self.records.pop(oldest["call_sid"])
            self.records[call_sid] = record

    def snapshot(self):
        with self._lock:
            records = sorted(self.records.values(), key=lambda r: (r["ended_at"] is None, r["started_at"]),
                             reverse=True)[:10]
            return {"enabled": self.enabled, "storage_error": self.storage_error,
                    "voicemails": deepcopy(records)}

    def _queue(self, record):
        with self._lock:
            self._dirty[record["call_sid"]] = deepcopy(record)
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._write())

    async def _write(self):
        while True:
            with self._lock:
                if not self._dirty:
                    return
                sid = next(iter(self._dirty))
                record = self._dirty.pop(sid)
                self._writing.add(sid)
            try:
                record["storage_error"] = ""
                await asyncio.to_thread(storage.save, self.path, record)
                with self._lock:
                    if sid in self.records:
                        self.records[sid]["storage_error"] = ""
            except (OSError, ValueError):
                with self._lock:
                    if sid in self.records:
                        self.records[sid]["storage_error"] = "save-failed"
            finally:
                with self._lock:
                    self._writing.discard(sid)

    def start(self, call_sid, reason, *, mode="voicemail_stub", started_at=None):
        with self._lock:
            if (not self.enabled or not SID.fullmatch(call_sid) or not isinstance(mode, str)
                    or mode not in {"voicemail_stub", "voicemail_ai", "voicemail_fallback"}):
                return False
            if call_sid in self.records:
                return True
            if len(self._dirty) >= 64:
                return False
            # Session admission already caps active calls at 16. Keep room for late
            # callbacks without growing forever; disk receipts have manual retention.
            while len(self.records) >= 64:
                finished = [r for r in self.records.values() if r["ended_at"]]
                if not finished:
                    return False
                oldest = min(finished, key=lambda r: r["started_at"])
                self.records.pop(oldest["call_sid"])
            record = {"schema_version": 1, "call_sid": call_sid, "mode": mode,
                      "reason": reason[:100], "started_at": started_at or now(), "ended_at": None,
                      "recording_status": "awaiting", "recording_sid": "",
                      "duration_seconds": None, "storage_error": ""}
            self.records[call_sid] = record
            self._queue(record)
            return True

    def fallback(self, call_sid, reason):
        """Change only the receipt: native Twilio Record owns this message now."""
        with self._lock:
            record = self.records.get(call_sid)
            if not record or record["mode"] != "voicemail_ai":
                return False
            record.update(mode="voicemail_fallback", reason=reason[:100],
                          recording_status="awaiting", recording_sid="", duration_seconds=None)
            self._queue(record)
            return True

    def finish_ai(self, call_sid, *, available, duration=None):
        """Local WAV completion is separate from Twilio cloud recording status."""
        with self._lock:
            record = self.records.get(call_sid)
            if not record or record["mode"] != "voicemail_ai":
                return
            record["ended_at"] = record["ended_at"] or now()
            record["recording_status"] = "completed" if available else "failed"
            record["duration_seconds"] = (int(duration) if type(duration) in (int, float)
                and 0 <= duration <= 86400 else None)
            self._queue(record)

    def fail_fallback(self, call_sid):
        with self._lock:
            record = self.records.get(call_sid)
            if record and record["mode"] == "voicemail_fallback":
                record.update(ended_at=record["ended_at"] or now(), recording_status="failed")
                self._queue(record)

    def finish(self, call_sid):
        with self._lock:
            record = self.records.get(call_sid)
            if record and record["ended_at"] is None:
                record["ended_at"] = now()
                if record["recording_status"] not in TERMINAL:
                    record["recording_status"] = "processing"
                self._queue(record)

    def recording(self, call_sid, recording_sid, status, duration=None):
        with self._lock:
            record = self.records.get(call_sid)
            if (not record or not RECORDING_SID.fullmatch(recording_sid)
                    or status not in {"in-progress", "completed", "absent", "failed"}
                    or (record["recording_sid"] and record["recording_sid"] != recording_sid)):
                return False
            if duration is not None and (type(duration) is not int or not 0 <= duration <= self.max_seconds + 5):
                return False
            if record["recording_status"] in TERMINAL:
                return True  # Duplicate/out-of-order callbacks never regress a receipt.
            record["recording_sid"] = recording_sid
            if status == "in-progress":
                if record["recording_status"] != "processing":
                    record["recording_status"] = "recording"
            else:
                record["recording_status"] = status
                record["ended_at"] = record["ended_at"] or now()
                record["duration_seconds"] = duration
            self._queue(record)
            return True

    async def close(self):
        work = [*self._loads, *([self._worker] if self._worker else [])]
        if work:
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.gather(*work, return_exceptions=True)), timeout=5)
            except TimeoutError:
                self.storage_error = "save-failed"
