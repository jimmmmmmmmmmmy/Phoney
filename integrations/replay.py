"""Bounded, offline playback of a completed Build 2 capture into local consumers."""

from contextlib import ExitStack
from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Iterator
import wave

from .contracts import AudioConsumer, AudioFrame, AudioTrack, CaptureFinished, NullConsumer


MAX_MANIFEST_BYTES = 64 * 1024
MAX_CAPTURE_SECONDS = 2 * 60 * 60
SAMPLE_RATE = 8000
MAX_SAMPLES = MAX_CAPTURE_SECONDS * SAMPLE_RATE
TRACKS = ("inbound", "outbound")


@dataclass(frozen=True, slots=True)
class CaptureTrack:
    name: AudioTrack
    meaning: str
    path: Path
    samples: int


@dataclass(frozen=True, slots=True)
class CompletedCapture:
    manifest_path: Path
    session_id: str
    stream_id: str
    tracks: tuple[CaptureTrack, ...]

    @property
    def duration_ms(self) -> int:
        return max(track.samples for track in self.tracks) * 1000 // SAMPLE_RATE


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"Capture manifest has an invalid {name}.")
    return value


def read_completed_capture(manifest_path: str | Path) -> CompletedCapture:
    """Check the completed marker and both WAV headers before yielding any audio.

    Only adjacent regular WAV files are accepted. A caller chooses the manifest
    locally; URLs, incomplete captures, path traversal, and symlink WAV files are
    not accepted. Reading is bounded to 64 KiB of metadata and two hours of audio.
    """
    path = Path(manifest_path).expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Capture manifest must be a file of at most 64 KiB.")
    with path.open("rb") as source:
        raw = source.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Capture manifest exceeds 64 KiB.")
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Capture manifest must contain valid JSON.") from exc
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1:
        raise ValueError("Expected capture manifest schema_version 1.")
    if manifest.get("status") != "completed":
        raise ValueError("Replay requires a completed capture; active or partial captures are not accepted.")
    expected_format = {"sample_rate": 8000, "channels": 1, "sample_width": 2, "encoding": "pcm_s16le"}
    if any(type(manifest.get(key)) is not type(value) or manifest.get(key) != value for key, value in expected_format.items()):
        raise ValueError("Capture must use 8000 Hz mono signed 16-bit little-endian PCM.")
    session_id = _identifier(manifest.get("call_sid"), "call_sid")
    stream_id = _identifier(manifest.get("stream_sid"), "stream_sid")
    metadata = manifest.get("tracks")
    if not isinstance(metadata, dict) or set(metadata) != set(TRACKS):
        raise ValueError("Capture must provide inbound and outbound tracks.")
    tracks = []
    for name in TRACKS:
        details = metadata[name]
        if not isinstance(details, dict):
            raise ValueError(f"Invalid {name} track metadata.")
        filename = details.get("file")
        if not isinstance(filename, str) or not filename or Path(filename).name != filename or filename in (".", ".."):
            raise ValueError("Capture WAV filenames must name adjacent files.")
        audio_path = path.parent / filename
        if audio_path.is_symlink() or not audio_path.is_file() or audio_path.resolve().parent != path.parent:
            raise ValueError("Capture WAV files must be regular files beside the manifest.")
        if audio_path.stat().st_size > MAX_SAMPLES * 2 + MAX_MANIFEST_BYTES:
            raise ValueError("Capture WAV exceeds the two-hour size bound.")
        try:
            with wave.open(str(audio_path), "rb") as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 8000, "NONE"):
                    raise ValueError("Capture WAV format differs from mono PCM16 at 8000 Hz.")
                samples = audio.getnframes()
        except (wave.Error, EOFError) as exc:
            raise ValueError(f"Invalid {name} WAV file.") from exc
        if samples > MAX_SAMPLES:
            raise ValueError("Capture WAV exceeds the two-hour duration bound.")
        declared_samples = details.get("samples")
        if isinstance(declared_samples, bool) or not isinstance(declared_samples, int) or declared_samples != samples:
            raise ValueError(f"Manifest sample count differs from {name} WAV header.")
        meaning = "caller-input" if name == "inbound" else "caller-playback"
        if details.get("meaning") != meaning:
            raise ValueError(f"Capture {name} track has an unexpected meaning.")
        tracks.append(CaptureTrack(name, meaning, audio_path, samples))
    return CompletedCapture(path, session_id, stream_id, tuple(tracks))


def iter_audio_frames(capture: CompletedCapture, *, frame_ms: int = 20) -> Iterator[AudioFrame]:
    """Yield timestamp-ordered PCM frames; WAV timelines are padded from t=0.

    Equal timestamps emit inbound before outbound. Frame sizes are synthesized
    for replay and need not match the original Twilio message boundaries.
    """
    if isinstance(frame_ms, bool) or not isinstance(frame_ms, int) or not 10 <= frame_ms <= 1000:
        raise ValueError("Replay frame size must be an integer from 10 to 1000 milliseconds.")
    frame_samples = SAMPLE_RATE * frame_ms // 1000
    with ExitStack() as stack:
        readers = {track.name: stack.enter_context(wave.open(str(track.path), "rb")) for track in capture.tracks}
        for offset in range(0, max(track.samples for track in capture.tracks), frame_samples):
            for track in capture.tracks:
                count = min(frame_samples, track.samples - offset)
                if count <= 0:
                    continue
                payload = readers[track.name].readframes(count)
                if len(payload) != count * 2:
                    raise ValueError(f"Capture {track.name} WAV ended before its declared length.")
                yield AudioFrame(capture.session_id, capture.stream_id, track.name,
                                 offset * 1000 // SAMPLE_RATE, payload)


def replay_capture(manifest_path: str | Path, consumer: AudioConsumer | None = None, *,
                   frame_ms: int = 20, realtime: bool = False) -> CaptureFinished:
    """Replay offline, then notify completion; consumer exceptions propagate.

    Default execution runs as fast as the consumer accepts frames. ``realtime``
    paces frames against their capture timestamps for local integration work.
    """
    capture = read_completed_capture(manifest_path)
    observer = consumer if consumer is not None else NullConsumer()
    started = time.monotonic()
    count = 0
    for frame in iter_audio_frames(capture, frame_ms=frame_ms):
        if realtime:
            remaining = started + frame.timestamp_ms / 1000 - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        observer.on_frame(frame)
        count += 1
    result = CaptureFinished(capture.session_id, capture.stream_id, capture.manifest_path, count)
    observer.on_end(result)
    return result
