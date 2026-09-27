"""Private, bounded advisory results; no audio, credentials, or call controls."""

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from .analysis import validate_analysis

SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
MAX_FILES = 1000
MAX_PUBLIC_RESULTS = 20
MAX_FILE_BYTES = 1024 * 1024
MAX_DETAIL_RECORDS = 8
MAX_COUNTER = 1_000_000
REFRESH_SECONDS = 2.0
REASONS = frozenset({
    "analyzing", "confident_synthetic", "confident_non_synthetic", "insufficient_evidence",
    "below_confidence_threshold", "conflicting_evidence", "no_usable_content", "no_results",
    "mixed_sessions", "dropped_audio", "audio_discontinuity", "incomplete_audio",
    "provider_incomplete", "provider_timeout", "provider_transport_failed",
    "provider_reported_error", "invalid_provider_response", "audio_collection_timeout",
    "cancelled", "internal_error", "capacity_exceeded", "too_many_streams", "interrupted",
})
RESULT_FIELDS = frozenset({
    "provider", "status", "label", "confidence", "reason", "streams", "observations",
    "accepted_frames", "dropped_frames", "submitted_audio_ms", "coverage_limited",
})
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _sid(value):
    return isinstance(value, str) and SID.fullmatch(value) is not None


def _root(path, create=False):
    if not path.is_absolute() or path == Path(path.anchor) or ".." in path.parts:
        raise ValueError("Detection storage must be an absolute private directory")
    parent = os.open(path.anchor, DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, DIR_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(part, DIR_FLAGS, dir_fd=parent)
            os.close(parent)
            parent = child
        if create:
            os.fchmod(parent, 0o700)
        return parent
    except BaseException:
        os.close(parent)
        raise


def _result(value):
    if (not isinstance(value, dict) or not RESULT_FIELDS.issubset(value)
            or set(value) - RESULT_FIELDS - {"analysis"}):
        raise ValueError("Invalid detection result")
    if (value["provider"] != "modulate" or value["status"] not in {"analyzing", "complete", "unknown"}
            or value["label"] not in {"synthetic", "non-synthetic", "unknown"}
            or value["reason"] not in REASONS or type(value["coverage_limited"]) is not bool):
        raise ValueError("Invalid detection state")
    confidence = value["confidence"]
    if confidence is not None and (type(confidence) not in (int, float)
            or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise ValueError("Invalid detection confidence")
    for field in ("streams", "observations", "accepted_frames", "dropped_frames", "submitted_audio_ms"):
        limit = 3_600_000 if field == "submitted_audio_ms" else MAX_COUNTER
        if type(value[field]) is not int or not 0 <= value[field] <= limit:
            raise ValueError("Invalid detection counter")
    if value["status"] != "complete" and (value["label"] != "unknown" or confidence is not None):
        raise ValueError("Incomplete detection cannot establish a verdict")
    if value["label"] != "unknown" and confidence is None:
        raise ValueError("A detection verdict requires confidence")
    normalized = {field: deepcopy(value[field]) for field in RESULT_FIELDS}
    if "analysis" in value:
        normalized["analysis"] = validate_analysis(value["analysis"])
    return normalized


def _document(value, call_sid):
    if (not isinstance(value, dict)
            or type(value.get("schema_version")) is not int or value["schema_version"] not in {1, 2}
            or set(value) != RESULT_FIELDS | {"schema_version", "call_sid", "updated_at"}
                | ({"analysis"} if value["schema_version"] == 2 else set())
            or value["call_sid"] != call_sid or not _sid(call_sid)):
        raise ValueError("Invalid detection document")
    stamp = value["updated_at"]
    if not isinstance(stamp, str) or not 20 <= len(stamp) <= 40:
        raise ValueError("Invalid detection timestamp")
    updated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if updated.tzinfo is None:
        raise ValueError("Detection timestamp requires a timezone")
    result = _result({field: item for field, item in value.items()
                      if field not in {"schema_version", "call_sid", "updated_at"}})
    return {**result, "schema_version": value["schema_version"], "call_sid": call_sid,
            "updated_at": updated.astimezone(timezone.utc).isoformat()}


class DetectionStore:
    """Synchronous methods belong in to_thread; failed storage never alters calls."""

    def __init__(self, storage_dir: str):
        self.path = Path(storage_dir)
        self.enabled = bool(storage_dir)
        self._records = {}
        self._details = OrderedDict()
        self._seen_versions = {}
        self._failed_writes = set()
        self._load_failed = False
        self._last_refresh = -float("inf")
        self._lock = threading.RLock()
        if self.enabled:
            self._refresh()

    @property
    def storage_error(self):
        return "storage-unavailable" if self._load_failed or self._failed_writes else ""

    def _read(self, root, call_sid):
        fd = os.open(call_sid + ".json", READ_FLAGS, dir_fd=root)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
                raise ValueError("Invalid detection file")
            with os.fdopen(fd, "rb", closefd=False) as source:
                raw = source.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("Detection file exceeds bound")
            return _document(json.loads(raw), call_sid)
        finally:
            os.close(fd)

    @staticmethod
    def _summary(record):
        result = {key: value for key, value in record.items() if key != "analysis"}
        if "analysis" in record:
            result["analysis"] = {key: value for key, value in record["analysis"].items() if key != "windows"}
        return result

    def _cache_detail(self, record):
        sid = record["call_sid"]
        self._details[sid] = record
        self._details.move_to_end(sid)
        while len(self._details) > MAX_DETAIL_RECORDS:
            self._details.popitem(last=False)

    def _merge(self, record):
        sid = record["call_sid"]
        current = self._records.get(sid)
        if sid in self._failed_writes or (current and current["updated_at"] > record["updated_at"]):
            return
        if current and current["updated_at"] == record["updated_at"]:
            # Retain live ownership/recovery state when reloading evicted detail.
            windows = record.get("analysis", {}).get("windows")
            record = deepcopy(current)
            if windows is not None:
                record["analysis"]["windows"] = windows
        elif record["status"] == "analyzing":
            record.update(status="unknown", label="unknown", confidence=None, reason="interrupted")
            if "analysis" in record:
                record["analysis"]["complete"] = False
        self._records[sid] = self._summary(record)
        self._cache_detail(record)
        self._bound()

    def _bound(self):
        while len(self._records) > MAX_FILES:
            removable = [record for sid, record in self._records.items() if sid not in self._failed_writes]
            if not removable:
                break
            oldest = min(removable, key=lambda record: record["updated_at"])
            self._records.pop(oldest["call_sid"])
            self._details.pop(oldest["call_sid"], None)
            self._seen_versions.pop(oldest["call_sid"], None)

    def _refresh(self):
        if not self.enabled or time.monotonic() - self._last_refresh < REFRESH_SECONDS:
            return
        self._last_refresh = time.monotonic()
        try:
            root = _root(self.path)
            try:
                with os.scandir(root) as entries:
                    for entry in islice(entries, MAX_FILES):
                        if not entry.name.endswith(".json") or not _sid(entry.name[:-5]):
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                            signature = (info.st_ino, info.st_mtime_ns, info.st_size)
                            sid = entry.name[:-5]
                            if self._seen_versions.get(sid) == signature:
                                continue
                            self._merge(self._read(root, sid))
                            self._seen_versions[sid] = signature
                        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                            continue
            finally:
                os.close(root)
            self._load_failed = False
        except FileNotFoundError:
            self._load_failed = False
        except (OSError, ValueError, TypeError):
            self._load_failed = True

    def save(self, call_sid: str, result: dict) -> bool:
        if not self.enabled or not _sid(call_sid):
            return False
        try:
            record = {**_result(result), "schema_version": 2 if "analysis" in result else 1, "call_sid": call_sid,
                      "updated_at": datetime.now(timezone.utc).isoformat()}
        except (ValueError, TypeError, OverflowError):
            return False
        with self._lock:
            previous = self._records.get(call_sid)
            # Concurrent background writes may finish out of order; a late start
            # notification must not replace the final result.
            if (previous and previous["status"] != "analyzing" and record["status"] == "analyzing"
                    and record["streams"] <= previous["streams"]):
                return True
            if call_sid not in self._records and len(self._failed_writes) >= MAX_FILES:
                return False
            self._records[call_sid] = self._summary(record)
            self._cache_detail(record)
            root = temporary = None
            try:
                raw = json.dumps(record, ensure_ascii=True, allow_nan=False).encode()
                if len(raw) > MAX_FILE_BYTES:
                    raise ValueError("Detection result exceeds bound")
                root = _root(self.path, create=True)
                try:
                    existing = os.stat(call_sid + ".json", dir_fd=root, follow_symlinks=False)
                    if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                        raise ValueError("Unsafe detection destination")
                except FileNotFoundError:
                    pass
                temporary = "." + call_sid + "." + secrets.token_hex(6) + ".tmp"
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             mode=0o600, dir_fd=root)
                with os.fdopen(fd, "wb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, call_sid + ".json", src_dir_fd=root, dst_dir_fd=root)
                self._failed_writes.discard(call_sid)
                self._load_failed = False
                return True
            except (OSError, ValueError, TypeError, OverflowError):
                self._failed_writes.add(call_sid)
                return False
            finally:
                if root is not None:
                    if temporary is not None:
                        try:
                            os.unlink(temporary, dir_fd=root)
                        except OSError:
                            pass
                    os.close(root)
                self._bound()

    @staticmethod
    def _public(record):
        return deepcopy({key: value for key, value in record.items() if key != "schema_version"})

    def get(self, call_sid: str) -> dict | None:
        if not self.enabled or not _sid(call_sid):
            return None
        with self._lock:
            try:
                root = _root(self.path)
                try:
                    self._merge(self._read(root, call_sid))
                finally:
                    os.close(root)
            except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                pass
            record = self._details.get(call_sid, self._records.get(call_sid))
            return self._public(record) if record is not None else None

    def snapshot(self) -> dict:
        with self._lock:
            self._refresh()
            recent = sorted(self._records.values(), key=lambda record: record["updated_at"], reverse=True)
            return {"enabled": self.enabled, "storage_error": self.storage_error,
                    "calls": [self._public(record) for record in recent[:MAX_PUBLIC_RESULTS]]}
