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

from transcription import storage

SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
RECORDING_SID = re.compile(r"RE[0-9a-fA-F]{32}\Z")
STATUSES = {"awaiting", "recording", "processing", "completed", "absent", "failed"}
TERMINAL = {"completed", "absent", "failed"}


def now():
    return datetime.now(timezone.utc).isoformat()


class VoicemailStore:
    """Bounded in-memory receipts and a serial, coalescing disk writer."""

    def __init__(self, settings):
        self.enabled = settings.voicemail_enabled
        self.path = settings.voicemail_storage_dir
        self.max_seconds = settings.voicemail_max_seconds
        self.records = {}
        self.storage_error = ""
        self._dirty = {}
        self._worker = None
        self._loads = set()
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
        if (set(item) != keys or item.get("schema_version") != 1
                or item.get("mode") != "voicemail_stub"
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
                type(item["duration_seconds"]) is not int or not 0 <= item["duration_seconds"] <= 605):
            return None
        if item["storage_error"] not in {"", "save-failed"}:
            return None
        return deepcopy(item)

    @property
    def active_count(self):
        return int(self._worker is not None and not self._worker.done()) + sum(not t.done() for t in self._loads)

    def _read_one(self, call_sid):
        """Late signed callbacks can refer to receipts outside the ten-row view."""
        root = storage.private_directory(Path(self.path))
        try:
            fd = os.open(call_sid + ".json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)
            with os.fdopen(fd, "rb") as source:
                data = source.read(8193)
            if len(data) > 8192:
                return None
            item = json.loads(data)
            if not isinstance(item, dict) or item.get("call_sid") != call_sid:
                return None
            return self._validated(item)
        finally:
            os.close(root)

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
        records = sorted(self.records.values(), key=lambda r: (r["ended_at"] is None, r["started_at"]),
                         reverse=True)[:10]
        return {"enabled": self.enabled, "storage_error": self.storage_error,
                "voicemails": deepcopy(records)}

    def _queue(self, record):
        self._dirty[record["call_sid"]] = deepcopy(record)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._write())

    async def _write(self):
        while self._dirty:
            sid = next(iter(self._dirty))
            record = self._dirty.pop(sid)
            try:
                record["storage_error"] = ""
                await asyncio.to_thread(storage.save, self.path, record)
                if sid in self.records:
                    self.records[sid]["storage_error"] = ""
            except (OSError, ValueError):
                if sid in self.records:
                    self.records[sid]["storage_error"] = "save-failed"

    def start(self, call_sid, reason):
        if not self.enabled or not SID.fullmatch(call_sid):
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
        record = {"schema_version": 1, "call_sid": call_sid, "mode": "voicemail_stub",
                  "reason": reason[:100], "started_at": now(), "ended_at": None,
                  "recording_status": "awaiting", "recording_sid": "",
                  "duration_seconds": None, "storage_error": ""}
        self.records[call_sid] = record
        self._queue(record)
        return True

    def finish(self, call_sid):
        record = self.records.get(call_sid)
        if record and record["ended_at"] is None:
            record["ended_at"] = now()
            if record["recording_status"] not in TERMINAL:
                record["recording_status"] = "processing"
            self._queue(record)

    def recording(self, call_sid, recording_sid, status, duration=None):
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
