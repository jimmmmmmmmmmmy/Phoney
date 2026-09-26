"""Bounded public playback of finalized captures, without exporting local paths.

The recorder writes a canonical 44-byte PCM WAV header. Requiring that exact
format lets arbitrary HTTP byte ranges map into a virtual stereo WAV without
decoding entire calls or creating another audio file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re
import stat
import struct
import threading
import time

from starlette.exceptions import HTTPException
from starlette.responses import Response, StreamingResponse

CALL_SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
STREAM_SID = re.compile(r"MZ[0-9a-fA-F]{32}\Z")
RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)\Z")
TRACKS = ("inbound", "outbound")
MAX_SCAN = 1000
MAX_RECORDINGS = 10
MAX_MANIFEST_BYTES = 65_536
MAX_SAMPLES = 3600 * 8000
CHUNK_BYTES = 32_768
CACHE_SECONDS = 2.0
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class _InvalidCapture(ValueError):
    pass


def _http_error(status_code, detail, headers=None):
    return HTTPException(status_code, detail, headers={"Cache-Control": "no-store",
                         "Referrer-Policy": "no-referrer", **(headers or {})})


def _open_root(path: Path) -> int:
    # Resolve every component using dir_fd. Path.resolve() would follow links
    # before we could enforce the storage boundary.
    if not path.is_absolute() or path == Path(path.anchor) or ".." in path.parts:
        raise _InvalidCapture("Invalid storage root")
    fd = os.open(path.anchor, DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_regular(directory_fd: int, name: str, maximum: int) -> tuple[int, os.stat_result]:
    # O_NONBLOCK ensures even an unexpected FIFO cannot hang before fstat().
    fd = os.open(name, FILE_FLAGS, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= maximum:
            raise _InvalidCapture("Invalid capture file")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _timestamp(value) -> str:
    if not isinstance(value, str) or not 20 <= len(value) <= 40:
        raise _InvalidCapture("Invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _InvalidCapture("Invalid timestamp") from None
    if parsed.tzinfo is None:
        raise _InvalidCapture("Missing timestamp timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _wav_header(samples: int, channels: int) -> bytes:
    size = samples * channels * 2
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", size + 36, b"WAVE", b"fmt ",
                       16, 1, channels, 8000, 8000 * channels * 2, channels * 2, 16,
                       b"data", size)


@dataclass(frozen=True)
class _Track:
    fd: int
    samples: int
    size: int
    modified_ns: int


class _Capture:
    def __init__(self, metadata, tracks):
        self.metadata = metadata
        self.tracks: dict[str, _Track] = tracks
        self._closed = False
        self._close_lock = threading.Lock()

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            for track in self.tracks.values():
                os.close(track.fd)

    def __del__(self):
        # Normally the ASGI response's finally block owns closure. Also release
        # handles if a caller constructs, but never sends, a response.
        self.close()

    @property
    def samples(self):
        return max(track.samples for track in self.tracks.values())

    def total(self, track):
        return 44 + self.samples * 4 if track == "combined" else self.tracks[track].size

    def etag(self, track):
        fingerprint = (track, self.metadata["call_sid"],
                       tuple((name, item.samples, item.size, item.modified_ns)
                             for name, item in self.tracks.items()))
        return '"' + hashlib.sha256(repr(fingerprint).encode()).hexdigest()[:32] + '"'

    def read(self, track, offset, count):
        # StreamingResponse reads these bounded chunks on its worker pool. Do
        # not close/reuse a descriptor while a disconnect overlaps that read.
        with self._close_lock:
            if self._closed:
                raise RuntimeError("Recording became unavailable")
            return self._read(track, offset, count)

    def _read(self, track, offset, count):
        if track != "combined":
            data = os.pread(self.tracks[track].fd, count, offset)
            if len(data) != count:
                raise RuntimeError("Recording became unavailable")
            return data
        output = bytearray()
        if offset < 44:
            header = _wav_header(self.samples, 2)
            output.extend(header[offset:min(44, offset + count)])
            offset += len(output)
            count -= len(output)
        if not count:
            return bytes(output)
        relative = offset - 44
        frame_start, skip = divmod(relative, 4)
        frame_count = (skip + count + 3) // 4
        stereo = bytearray(frame_count * 4)
        for channel, name in enumerate(TRACKS):
            item = self.tracks[name]
            available = max(0, min(frame_count, item.samples - frame_start))
            mono = os.pread(item.fd, available * 2, 44 + frame_start * 2) if available else b""
            if len(mono) != available * 2:
                raise RuntimeError("Recording became unavailable")
            # Byte-level interleave preserves little-endian samples on every
            # host and handles byte ranges that begin midway through a sample.
            stereo[channel * 2:available * 4:4] = mono[0::2]
            stereo[channel * 2 + 1:available * 4:4] = mono[1::2]
        output.extend(stereo[skip:skip + count])
        return bytes(output)

    def chunks(self, track, start, end):
        offset = start
        while offset <= end:
            count = min(CHUNK_BYTES, end - offset + 1)
            yield self.read(track, offset, count)
            offset += count


class _AudioResponse(StreamingResponse):
    def __init__(self, capture, track, start, end, *, status_code, headers):
        self.capture = capture
        super().__init__(capture.chunks(track, start, end), status_code=status_code,
                         headers=headers, media_type="audio/wav")

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.capture.close()


def _range(value, total):
    if not value or len(value) > 100:
        raise _InvalidCapture("Invalid byte range")
    match = RANGE.fullmatch(value.strip())
    if not match or not any(match.groups()):
        raise _InvalidCapture("Invalid byte range")
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix == 0:
            raise _InvalidCapture("Empty byte range")
        return max(0, total - suffix), total - 1
    start = int(first)
    end = min(int(last), total - 1) if last else total - 1
    if start >= total or start > end:
        raise _InvalidCapture("Unsatisfied byte range")
    return start, end


class RecordingLibrary:
    """Read only finalized on-disk WAVs; unrelated transcripts are never needed."""

    def __init__(self, settings):
        self.root = Path(settings.media_storage_dir)
        # Capturing may be paused while existing recordings remain playable.
        self.enabled = bool(settings.media_storage_dir and self.root.is_absolute())
        self._cache = None
        self._cached_at = 0.0
        self._cache_lock = threading.Lock()

    def _open(self, call_sid, root_fd=None):
        if not isinstance(call_sid, str) or not CALL_SID.fullmatch(call_sid):
            raise _InvalidCapture("Invalid call identifier")
        own_root = root_fd is None
        root = _open_root(self.root) if own_root else root_fd
        directory = None
        tracks = {}
        try:
            directory = os.open(call_sid, DIRECTORY_FLAGS, dir_fd=root)
            manifest_fd, info = _open_regular(directory, "manifest.json", MAX_MANIFEST_BYTES)
            try:
                raw = os.pread(manifest_fd, MAX_MANIFEST_BYTES + 1, 0)
            finally:
                os.close(manifest_fd)
            if len(raw) != info.st_size:
                raise _InvalidCapture("Unstable manifest")
            manifest = json.loads(raw)
            if not isinstance(manifest, dict):
                raise _InvalidCapture("Invalid manifest")
            expected = {"schema_version": 1, "call_sid": call_sid, "sample_rate": 8000,
                        "channels": 1, "sample_width": 2, "encoding": "pcm_s16le"}
            if any(type(manifest.get(key)) is not type(value) or manifest.get(key) != value
                   for key, value in expected.items()):
                raise _InvalidCapture("Invalid manifest format")
            stream = manifest.get("stream_sid")
            if not isinstance(stream, str) or not STREAM_SID.fullmatch(stream):
                raise _InvalidCapture("Invalid stream identifier")
            if manifest.get("status") not in {"completed", "partial"}:
                raise _InvalidCapture("Capture is not finalized")
            started, finished = _timestamp(manifest.get("started_at")), _timestamp(manifest.get("finished_at"))
            if finished < started:
                raise _InvalidCapture("Invalid capture times")
            details = manifest.get("tracks")
            if not isinstance(details, dict) or set(details) != set(TRACKS):
                raise _InvalidCapture("Invalid tracks")
            for name in TRACKS:
                item = details[name]
                meaning = "caller-input" if name == "inbound" else "caller-playback"
                if (not isinstance(item, dict) or item.get("file") != name + ".wav"
                        or item.get("meaning") != meaning or type(item.get("samples")) is not int
                        or not 0 <= item["samples"] <= MAX_SAMPLES):
                    raise _InvalidCapture("Invalid track format")
                fd, file_info = _open_regular(directory, name + ".wav", MAX_SAMPLES * 2 + 44)
                tracks[name] = _Track(fd, item["samples"], file_info.st_size, file_info.st_mtime_ns)
                if (file_info.st_size != 44 + item["samples"] * 2
                        or os.pread(fd, 44, 0) != _wav_header(item["samples"], 1)):
                    raise _InvalidCapture("Invalid WAV format")
            if max(track.samples for track in tracks.values()) == 0:
                raise _InvalidCapture("Empty capture")
            metadata = {"call_sid": call_sid, "started_at": started, "finished_at": finished,
                        "status": manifest["status"],
                        "duration_seconds": max(track.samples for track in tracks.values()) / 8000,
                        "url": f"/api/recordings/{call_sid}/audio?track=combined",
                        "tracks": {name: {"duration_seconds": track.samples / 8000,
                            "url": f"/api/recordings/{call_sid}/audio?track={name}"}
                            for name, track in tracks.items()}}
            return _Capture(metadata, tracks)
        except BaseException:
            for track in tracks.values():
                os.close(track.fd)
            raise
        finally:
            if directory is not None:
                os.close(directory)
            if own_root:
                os.close(root)

    def snapshot(self):
        with self._cache_lock:
            if self._cache is not None and time.monotonic() - self._cached_at < CACHE_SECONDS:
                return copy.deepcopy(self._cache)
            result = {"enabled": self.enabled, "recordings": [], "storage_error": ""}
            if self.enabled:
                try:
                    root = _open_root(self.root)
                    try:
                        with os.scandir(root) as entries:
                            for entry in islice(entries, MAX_SCAN):
                                if not CALL_SID.fullmatch(entry.name):
                                    continue
                                try:
                                    capture = self._open(entry.name, root)
                                except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                                    continue
                                try:
                                    result["recordings"].append(capture.metadata)
                                finally:
                                    capture.close()
                    finally:
                        os.close(root)
                    result["recordings"].sort(key=lambda item: item["finished_at"], reverse=True)
                    result["recordings"] = result["recordings"][:MAX_RECORDINGS]
                except FileNotFoundError:
                    # The recorder creates its root lazily on the first call.
                    pass
                except (OSError, ValueError):
                    result["storage_error"] = "storage-unavailable"
            self._cache, self._cached_at = result, time.monotonic()
            return copy.deepcopy(result)

    def response(self, call_sid, track, request):
        if request.method not in {"GET", "HEAD"}:
            raise _http_error(405, "Method not allowed", {"Allow": "GET, HEAD"})
        if not self.enabled:
            raise _http_error(404, "Recording not found")
        if track not in {*TRACKS, "combined"}:
            raise _http_error(400, "Unknown audio track")
        try:
            capture = self._open(call_sid)
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            raise _http_error(404, "Recording not found") from None
        try:
            total = capture.total(track)
            headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store", "ETag": capture.etag(track),
                       "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                       "Content-Disposition": f'inline; filename="{call_sid}-{track}.wav"'}
            start, end, code = 0, total - 1, 200
            requested = request.headers.get("range")
            if (request.method == "GET" and requested is not None
                    and request.headers.get("if-range", headers["ETag"]) == headers["ETag"]):
                try:
                    start, end = _range(requested, total)
                except _InvalidCapture:
                    capture.close()
                    headers["Content-Range"] = f"bytes */{total}"
                    return Response(status_code=416, headers=headers, media_type="audio/wav")
                code = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            headers["Content-Length"] = str(end - start + 1)
            if request.method == "HEAD":
                capture.close()
                return Response(status_code=code, headers=headers, media_type="audio/wav")
            return _AudioResponse(capture, track, start, end, status_code=code, headers=headers)
        except BaseException:
            capture.close()
            raise
