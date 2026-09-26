"""Private bounded transcript JSON storage; audio and provider credentials stay out."""

import json
import os
from pathlib import Path
import re
import secrets

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_HISTORY = 10
MAX_SCAN = 1000
SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")


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
