"""Private, bounded caller details and transcript-bound agent summaries.

All public methods fail open: call routing must not depend on this metadata
store. Atomic files also allow a local helper process to backfill old calls.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
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

SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
STREAM_SID = re.compile(r"MZ[0-9a-fA-F]{32}\Z")
E164 = re.compile(r"\+[1-9][0-9]{7,14}\Z")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_FILES = 1000
MAX_CALLS = 10
MAX_FILE_BYTES = 16_384
MAX_SUMMARY_CHARS = 2000
MAX_SUMMARY_ATTEMPTS = 5
SUMMARY_MODEL = re.compile(r"[a-z0-9][a-z0-9.-]{0,99}\Z")
SUMMARY_ERRORS = {"", "auth", "rate-limit", "provider-error", "timeout", "network", "invalid-response",
                  "blocked", "empty-response", "truncated", "invalid-input", "storage-failed",
                  "cancelled", "unavailable", "stale", "unknown", "invalid_transcript", "input_too_large",
                  "invalid_response", "no_output", "invalid_output", "closed", "not_configured",
                  "invalid_configuration", "rate_limited", "provider_unavailable", "authentication_failed",
                  "request_rejected", "response_too_large", "transport_error", "billing_required",
                  "retry-exhausted", "interrupted", "transcript-changed", "provider-timeout", "storage-error"}
MAX_DURATION_SECONDS = 86_400
REFRESH_SECONDS = 2.0
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sid(value):
    return isinstance(value, str) and SID.fullmatch(value) is not None


def _timestamp(value):
    if not isinstance(value, str) or not 20 <= len(value) <= 40:
        raise ValueError("Invalid timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return stamp.astimezone(timezone.utc).isoformat()


def _duration(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= MAX_DURATION_SECONDS):
        raise ValueError("Invalid duration")
    return round(value, 3)


def normalize_caller_number(value):
    """Accept an explicit country code, never guess one for local numbers."""
    if not isinstance(value, str) or len(value) > 64:
        return ""
    value = value.strip(" ")
    if not re.fullmatch(r"\+[0-9 ()\.\-]+", value):
        return ""
    compact = re.sub(r"[ ()\.\-]", "", value)
    return compact if E164.fullmatch(compact) else ""


def transcript_fingerprint(document):
    """Fingerprint finalized text/timing, excluding interims and confidence.

An ended transcript with at least one final nonblank segment is required. This
is a content identity, not a claim that its speech recognition is accurate.
"""
    try:
        if not isinstance(document, dict) or not _sid(document.get("call_sid")):
            return None
        _timestamp(document.get("ended_at"))
        stream = document.get("stream_sid", "")
        if stream and (not isinstance(stream, str) or not STREAM_SID.fullmatch(stream)):
            return None
        segments = document.get("segments")
        if not isinstance(segments, list) or not 1 <= len(segments) <= 2000:
            return None
        rows, characters = [], 0
        for segment in segments:
            if not isinstance(segment, dict):
                return None
            track, start, end, text = (segment.get(key) for key in ("track", "start_ms", "end_ms", "text"))
            if (track not in ("inbound", "outbound") or type(start) is not int or type(end) is not int
                    or not 0 <= start <= end <= 3_600_000 or not isinstance(text, str)
                    or not text.strip() or len(text) > 2000):
                return None
            characters += len(text)
            if characters > 100_000:
                return None
            rows.append([track, start, end, text])
        rows.sort(key=lambda row: (row[1], row[2], row[0], row[3]))
        payload = json.dumps([document["call_sid"], stream, rows], ensure_ascii=True,
                             separators=(",", ":"), allow_nan=False).encode()
        return hashlib.sha256(payload).hexdigest()
    except (ValueError, TypeError, OverflowError):
        return None


def _root(path, create=False):
    if not path.is_absolute() or path == Path(path.anchor) or ".." in path.parts:
        raise ValueError("Invalid details storage directory")
    parent = os.open(path.anchor, DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            if create:
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


def _blank(call_sid):
    return {"schema_version": 1, "call_sid": call_sid, "caller_number": "", "started_at": None,
            "ended_at": None, "duration_seconds": None, "duration_source": "", "summary": None,
            "summary_job": None}


def _validate(document, call_sid):
    # Existing authored summaries predate background jobs and remain readable.
    if isinstance(document, dict) and "summary_job" not in document:
        document["summary_job"] = None
    keys = set(_blank(call_sid))
    if (not isinstance(document, dict) or set(document) != keys
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or document["call_sid"] != call_sid or not _sid(call_sid)):
        raise ValueError("Invalid details document")
    number = document["caller_number"]
    if not isinstance(number, str) or (number and not E164.fullmatch(number)):
        raise ValueError("Invalid caller number")
    for key in ("started_at", "ended_at"):
        if document[key] is not None:
            document[key] = _timestamp(document[key])
    if document["started_at"] and document["ended_at"] and document["ended_at"] < document["started_at"]:
        raise ValueError("Invalid call times")
    if document["duration_seconds"] is not None:
        document["duration_seconds"] = _duration(document["duration_seconds"])
    if document["duration_source"] not in ("", "elapsed", "reported"):
        raise ValueError("Invalid duration source")
    summary = document["summary"]
    if summary is not None:
        if (not isinstance(summary, dict)
                or set(summary) - {"text", "source", "created_at", "fingerprint", "model"}
                or not {"text", "source", "created_at", "fingerprint"}.issubset(summary)
                or not isinstance(summary["text"], str) or not summary["text"].strip()
                or len(summary["text"]) > MAX_SUMMARY_CHARS or CONTROL.search(summary["text"])
                or summary["source"] not in {"agent", "gemini"} or not isinstance(summary["fingerprint"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", summary["fingerprint"])):
            raise ValueError("Invalid summary")
        summary["created_at"] = _timestamp(summary["created_at"])
        if "model" in summary and (not isinstance(summary["model"], str)
                                   or not SUMMARY_MODEL.fullmatch(summary["model"])):
            raise ValueError("Invalid summary model")
    job = document["summary_job"]
    if job is not None:
        if (not isinstance(job, dict) or set(job) != {"fingerprint", "status", "attempts", "error", "retry_at"}
                or not isinstance(job["fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", job["fingerprint"])
                or job["status"] not in {"pending", "completed", "failed"}
                or type(job["attempts"]) is not int or not 0 <= job["attempts"] <= MAX_SUMMARY_ATTEMPTS
                or job["error"] not in SUMMARY_ERRORS
                or type(job["retry_at"]) not in (int, float) or not math.isfinite(job["retry_at"])
                or not 0 <= job["retry_at"] <= 10_000_000_000):
            raise ValueError("Invalid summary job")
    return document


class CallDetailsStore:
    def __init__(self, storage_dir: str):
        self.path = Path(storage_dir)
        self.enabled = bool(storage_dir)
        self._records = {}
        self._failed_writes = set()
        self._load_failed = False
        self._lock = threading.RLock()
        self._last_refresh = float("-inf")
        if self.enabled:
            with self._lock:
                self._refresh()

    @property
    def storage_error(self):
        return "save-failed" if self._failed_writes else "load-failed" if self._load_failed else ""

    def _read(self, root, call_sid):
        fd = os.open(call_sid + ".json", READ_FLAGS, dir_fd=root)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE_BYTES:
                raise ValueError("Invalid details file")
            raw = os.pread(fd, MAX_FILE_BYTES + 1, 0)
            if len(raw) != info.st_size:
                raise ValueError("Unstable details file")
            return _validate(json.loads(raw), call_sid)
        finally:
            os.close(fd)

    def _merge_disk(self, document):
        sid = document["call_sid"]
        current = self._records.get(sid)
        if sid in self._failed_writes:
            return
        if current is None and len(self._records) >= MAX_FILES:
            ended = [item for item in self._records.values()
                     if item["ended_at"] and item["call_sid"] not in self._failed_writes]
            if not ended:
                return
            self._records.pop(min(ended, key=lambda item: item["ended_at"])["call_sid"])
        if current and current["ended_at"] is None:
            # An external backfill must not turn this process's active call
            # into a completed one. Its local finish callback owns that step.
            merged = deepcopy(document)
            merged.update({key: current[key] for key in ("started_at", "ended_at", "duration_seconds", "duration_source")})
            merged["caller_number"] = current["caller_number"] or document["caller_number"]
            document = merged
        self._records[sid] = document

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
                            self._merge_disk(self._read(root, entry.name[:-5]))
                        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                            continue
            finally:
                os.close(root)
            self._load_failed = False
            self._bound()
        except FileNotFoundError:
            self._load_failed = False
        except (OSError, ValueError, TypeError):
            self._load_failed = True

    def _bound(self):
        while len(self._records) > MAX_FILES:
            ended = [item for item in self._records.values()
                     if item["ended_at"] and item["call_sid"] not in self._failed_writes]
            if not ended:
                return False
            oldest = min(ended, key=lambda item: item["ended_at"])
            self._records.pop(oldest["call_sid"])
        return True

    def _current(self, call_sid):
        # Read this one file before mutating so a helper's saved summary is not
        # discarded merely because the two-second catalog cache has not expired.
        if call_sid not in self._failed_writes:
            try:
                root = _root(self.path)
                try:
                    self._merge_disk(self._read(root, call_sid))
                finally:
                    os.close(root)
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                self._load_failed = True
        return deepcopy(self._records.get(call_sid, _blank(call_sid)))

    def _persist(self, record):
        sid = record["call_sid"]
        if sid not in self._records and len(self._records) >= MAX_FILES:
            ended = [item for item in self._records.values()
                     if item["ended_at"] and item["call_sid"] not in self._failed_writes]
            if not ended:
                return False
            self._records.pop(min(ended, key=lambda item: item["ended_at"])["call_sid"])
        self._records[sid] = deepcopy(record)
        root = temporary = None
        try:
            raw = json.dumps(record, ensure_ascii=True, allow_nan=False).encode()
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("Details file exceeds bound")
            root = _root(self.path, create=True)
            try:
                info = os.stat(sid + ".json", dir_fd=root, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Invalid details destination")
            except FileNotFoundError:
                pass
            temporary = f".{sid}.{secrets.token_hex(8)}.tmp"
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         mode=0o600, dir_fd=root)
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, sid + ".json", src_dir_fd=root, dst_dir_fd=root)
            self._failed_writes.discard(sid)
            return True
        except (OSError, ValueError, TypeError, OverflowError):
            self._failed_writes.add(sid)
            return False
        finally:
            if root is not None:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=root)
                    except OSError:
                        pass
                os.close(root)

    def start(self, call_sid, caller_number, started_at=None):
        if not self.enabled or not _sid(call_sid):
            return False
        try:
            explicit_start = _timestamp(started_at) if started_at is not None else None
            with self._lock:
                record = self._current(call_sid)
                if record["started_at"] is None:
                    proposed = explicit_start or (None if record["ended_at"] else _now())
                    if proposed and record["ended_at"] and proposed > record["ended_at"]:
                        return False
                    record["started_at"] = proposed
                record["caller_number"] = record["caller_number"] or normalize_caller_number(caller_number)
                return self._persist(record)
        except (OSError, ValueError, TypeError, OverflowError):
            return False

    def finish(self, call_sid, ended_at=None, duration_seconds=None):
        if not self.enabled or not _sid(call_sid):
            return False
        try:
            end = _timestamp(ended_at) if ended_at is not None else _now()
            duration = _duration(duration_seconds) if duration_seconds is not None else None
            with self._lock:
                record = self._current(call_sid)
                if record["ended_at"] is None:
                    if record["started_at"] and end < record["started_at"]:
                        return False
                    record["ended_at"] = end
                if duration is not None:
                    record["duration_seconds"], record["duration_source"] = duration, "reported"
                elif record["duration_seconds"] is None and record["started_at"]:
                    elapsed = (datetime.fromisoformat(record["ended_at"]) - datetime.fromisoformat(record["started_at"])).total_seconds()
                    if 0 <= elapsed <= MAX_DURATION_SECONDS:
                        record["duration_seconds"], record["duration_source"] = round(elapsed, 3), "elapsed"
                return self._persist(record)
        except (OSError, ValueError, TypeError, OverflowError):
            return False

    def set_summary(self, call_sid, text, session_document, *, source="agent", model=None):
        if (not self.enabled or not _sid(call_sid) or not isinstance(text, str)
                or not text.strip() or len(text) > MAX_SUMMARY_CHARS or CONTROL.search(text)
                or not isinstance(session_document, dict) or session_document.get("call_sid") != call_sid
                or source not in {"agent", "gemini"}
                or (model is not None and (not isinstance(model, str) or not SUMMARY_MODEL.fullmatch(model)))):
            return False
        fingerprint = transcript_fingerprint(session_document)
        if not fingerprint:
            return False
        try:
            with self._lock:
                record = self._current(call_sid)
                if record["started_at"] is None and session_document.get("started_at"):
                    record["started_at"] = _timestamp(session_document["started_at"])
                record["ended_at"] = record["ended_at"] or _timestamp(session_document["ended_at"])
                if record["started_at"] and record["ended_at"] < record["started_at"]:
                    return False
                previous = record["summary"]
                if (source == "gemini" and previous and previous["source"] == "agent"
                        and previous["fingerprint"] == fingerprint):
                    return True  # Preserve an operator edit made while generation was in flight.
                if (not previous or previous["text"] != text.strip() or previous["fingerprint"] != fingerprint
                        or previous["source"] != source or previous.get("model") != model):
                    record["summary"] = {"text": text.strip(), "source": source, "created_at": _now(),
                                         "fingerprint": fingerprint}
                    if model is not None:
                        record["summary"]["model"] = model
                job = record["summary_job"]
                record["summary_job"] = {"fingerprint": fingerprint, "status": "completed",
                    "attempts": job["attempts"] if job and job["fingerprint"] == fingerprint else 0,
                    "error": "", "retry_at": 0}
                return self._persist(record)
        except (OSError, ValueError, TypeError, OverflowError):
            return False

    @staticmethod
    def _summary_state(record, fingerprint):
        missing = {"status": "missing", "attempts": 0, "error": "", "retry_at": 0}
        if not fingerprint:
            return missing
        summary = record["summary"]
        job = record["summary_job"]
        if summary and summary["fingerprint"] == fingerprint:
            return {**missing, "status": "completed"}
        if job and job["fingerprint"] == fingerprint:
            return {key: job[key] for key in missing}
        return missing

    def summary_state(self, call_sid, session_document):
        fingerprint = transcript_fingerprint(session_document)
        if not self.enabled or not _sid(call_sid) or not fingerprint or session_document["call_sid"] != call_sid:
            return {"status": "missing", "attempts": 0, "error": "", "retry_at": 0}
        with self._lock:
            return self._summary_state(self._current(call_sid), fingerprint)

    def begin_summary(self, call_sid, session_document):
        fingerprint = transcript_fingerprint(session_document)
        if not self.enabled or not _sid(call_sid) or not fingerprint or session_document["call_sid"] != call_sid:
            return False
        try:
            with self._lock:
                record = self._current(call_sid)
                job = self._summary_state(record, fingerprint)
                if job["status"] == "completed" or job["attempts"] >= MAX_SUMMARY_ATTEMPTS:
                    return False
                if job["status"] == "failed" and (not job["retry_at"] or job["retry_at"] > time.time()):
                    return False
                record["started_at"] = record["started_at"] or _timestamp(session_document["started_at"])
                record["ended_at"] = record["ended_at"] or _timestamp(session_document["ended_at"])
                record["summary_job"] = {"fingerprint": fingerprint, "status": "pending",
                    "attempts": job["attempts"] + 1, "error": "", "retry_at": 0}
                return self._persist(record)
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return False

    def fail_summary(self, call_sid, session_document, error, retry_at=0):
        fingerprint = transcript_fingerprint(session_document)
        if (not self.enabled or not _sid(call_sid) or not fingerprint or session_document["call_sid"] != call_sid
                or type(retry_at) not in (int, float) or not math.isfinite(retry_at)
                or not 0 <= retry_at <= 10_000_000_000):
            return False
        with self._lock:
            record = self._current(call_sid)
            job = record["summary_job"]
            if not job or job["fingerprint"] != fingerprint or job["status"] == "completed":
                return False
            job.update(status="failed", error=error if isinstance(error, str) and error in SUMMARY_ERRORS else "unknown",
                       retry_at=retry_at)
            return self._persist(record)

    def retry_summary(self, call_sid, session_document):
        """Owner-invoked retry after repairing billing or configuration; never a public route."""
        fingerprint = transcript_fingerprint(session_document)
        if not self.enabled or not _sid(call_sid) or not fingerprint or session_document["call_sid"] != call_sid:
            return False
        with self._lock:
            record = self._current(call_sid)
            state = self._summary_state(record, fingerprint)
            if state["status"] != "failed":
                return False
            record["summary_job"] = None
            return self._persist(record)

    def snapshot(self, sessions=None):
        if not self.enabled:
            return {"enabled": False, "storage_error": "", "calls": []}
        with self._lock:
            self._refresh()
            documents = {}
            if isinstance(sessions, list):
                for document in sessions[:MAX_FILES]:
                    if isinstance(document, dict) and _sid(document.get("call_sid")):
                        documents[document["call_sid"]] = document
            records = deepcopy(self._records)
            for sid, document in documents.items():
                if sid not in records:
                    records[sid] = _blank(sid)
                record = records[sid]
                for key, fallback in (("started_at", "started_at"), ("ended_at", "finished_at")):
                    if record[key] is None:
                        value = document.get(key) or document.get(fallback)
                        if value:
                            try:
                                record[key] = _timestamp(value)
                            except (ValueError, TypeError, OverflowError):
                                pass
                if record["duration_seconds"] is None:
                    try:
                        value = document.get("duration_seconds")
                        if value is not None:
                            record["duration_seconds"] = _duration(value)
                        elif record["started_at"] and record["ended_at"]:
                            record["duration_seconds"] = _duration((datetime.fromisoformat(record["ended_at"])
                                - datetime.fromisoformat(record["started_at"])).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
            recent = sorted(records.values(), key=lambda item: (
                item["call_sid"] in documents, item["started_at"] or item["ended_at"] or ""), reverse=True)[:MAX_CALLS]
            calls = []
            for record in recent:
                result = {key: record[key] for key in ("call_sid", "caller_number", "started_at", "ended_at", "duration_seconds")}
                summary = record["summary"]
                fingerprint = transcript_fingerprint(documents.get(record["call_sid"])) if summary else None
                result["summary"] = ({key: summary[key] for key in ("text", "source", "created_at")}
                    if summary and record["ended_at"] and fingerprint == summary["fingerprint"] else None)
                if result["summary"] and "model" in summary:
                    result["summary"]["model"] = summary["model"]
                job_fingerprint = transcript_fingerprint(documents.get(record["call_sid"]))
                job_state = self._summary_state(record, job_fingerprint)
                if record["summary_job"] is not None:
                    result["summary_status"] = job_state["status"]
                    if (job_state["status"] == "failed" and job_state["retry_at"] > 0
                            and job_state["attempts"] < MAX_SUMMARY_ATTEMPTS):
                        result["summary_status"] = "retrying"
                        result["summary_retry_at"] = job_state["retry_at"]
                calls.append(result)
            return {"enabled": True, "storage_error": self.storage_error, "calls": calls}
