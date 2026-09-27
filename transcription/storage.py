"""Private bounded transcript JSON storage; audio and provider credentials stay out."""

import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from copy import deepcopy

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_HISTORY = 10
MAX_SCAN = 1000
SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")


def _read(root, call_sid):
    fd = os.open(call_sid + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                 dir_fd=root)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE_BYTES:
            return None
        data = os.pread(fd, MAX_FILE_BYTES + 1, 0)
        if len(data) != info.st_size:
            return None
        document = json.loads(data)
        return document if isinstance(document, dict) and document.get("call_sid") == call_sid else None
    finally:
        os.close(fd)


def load_call(path: str, call_sid: str) -> dict | None:
    """Read one saved call independently of the hot history or directory order."""
    if not isinstance(call_sid, str) or not SID.fullmatch(call_sid):
        return None
    if not path or not Path(path).exists():
        return None
    root = private_directory(Path(path))
    try:
        try:
            return _read(root, call_sid)
        except (OSError, ValueError, TypeError, RecursionError):
            return None
    finally:
        os.close(root)


def _entries(root):
    with os.scandir(root) as entries:
        for entry in entries:
            if not entry.name.endswith(".json") or not SID.fullmatch(entry.name[:-5]):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_FILE_BYTES:
                    yield entry.name[:-5], (info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
            except OSError:
                continue


def list_call_ids(path: str) -> list[str]:
    """List identifiers only; callers bound their own document reads per batch."""
    if not path or not Path(path).exists():
        return []
    root = private_directory(Path(path))
    try:
        return sorted(sid for sid, _ in _entries(root))
    finally:
        os.close(root)


class ArchiveIndex:
    """Cache small validated headers, never an archive of transcript text in RAM.

    Directory enumeration has no age cutoff. Unchanged files are not reparsed;
    HTTP responses and full-document reads remain page bounded.
    """
    def __init__(self, path, validator):
        self.path = path
        self.validator = validator
        self._items = {}
        self._lock = threading.Lock()
        self._refreshed = float("-inf")
        self._error = ""

    def snapshot(self):
        with self._lock:
            if time.monotonic() - self._refreshed >= 2:
                self._refreshed = time.monotonic()
                try:
                    self._refresh()
                    self._error = "partial-history" if any(item[1] is None for item in self._items.values()) else ""
                except (OSError, ValueError, TypeError):
                    self._error = "storage-unavailable"
            return {"storage_error": self._error,
                    "sessions": deepcopy([item[1] for item in self._items.values() if item[1]])}

    def _refresh(self):
        if not self.path or not Path(self.path).exists():
            self._items = {}
            return
        root = private_directory(Path(self.path))
        try:
            seen = set()
            for sid, signature in _entries(root):
                seen.add(sid)
                if sid in self._items and self._items[sid][0] == signature:
                    continue
                header = None
                try:
                    document = _read(root, sid)
                    if document and self.validator(document):
                        header = {key: deepcopy(document[key]) for key in
                                  ("call_sid", "stream_sid", "started_at", "ended_at", "status",
                                   "model", "finish_reason", "storage_error", "tracks")}
                        header["segments"] = []
                except (OSError, ValueError, TypeError, RecursionError):
                    pass
                self._items[sid] = (signature, header)
            self._items = {sid: item for sid, item in self._items.items() if sid in seen}
        finally:
            os.close(root)


def private_directory(path: Path) -> int:
    if not path.is_absolute() or path == Path(path.anchor) or ".." in path.parts:
        raise ValueError("Transcript storage must be an absolute private directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=parent)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        os.fchmod(parent, 0o700)
        return parent
    except BaseException:
        os.close(parent)
        raise


def save(path: str, document: dict) -> None:
    sid = document["call_sid"]
    if not SID.fullmatch(sid):
        raise ValueError("Invalid transcript identifier")
    data = json.dumps(document, ensure_ascii=True, allow_nan=False).encode()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Transcript exceeds storage bound")
    root = private_directory(Path(path))
    temporary = f".{sid}.{secrets.token_hex(6)}.tmp"
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                     mode=0o600, dir_fd=root)
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, sid + ".json", src_dir_fd=root, dst_dir_fd=root)
    finally:
        try:
            os.unlink(temporary, dir_fd=root)
        except FileNotFoundError:
            pass
        os.close(root)


def load(path: str) -> list[dict]:
    """Bound directory scanning and reads; schema validation belongs to service."""
    if not Path(path).exists():
        return []
    root = private_directory(Path(path))
    try:
        candidates = []
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_SCAN:
                    break
                if (entry.name.endswith(".json") and SID.fullmatch(entry.name[:-5])
                        and entry.is_file(follow_symlinks=False)):
                    info = entry.stat(follow_symlinks=False)
                    if info.st_size <= MAX_FILE_BYTES:
                        candidates.append((info.st_mtime_ns, entry.name))
        documents = []
        for _, name in sorted(candidates, reverse=True)[:MAX_HISTORY]:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)
                with os.fdopen(fd, "rb") as source:
                    data = source.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    continue
                document = json.loads(data)
                if isinstance(document, dict) and document.get("call_sid") + ".json" == name:
                    documents.append(document)
            except (OSError, ValueError, TypeError):
                continue
        return documents
    finally:
        os.close(root)
