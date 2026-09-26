"""Local partner interfaces for Build 2 audio replay; no provider implementation."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol


AudioTrack = Literal["inbound", "outbound"]


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """One mono PCM frame on the caller leg's media timeline.

    ``inbound`` is caller input. ``outbound`` is what the caller heard, which
    can include conference audio and hold audio. Neither is a named speaker.
    PCM bytes are signed 16-bit little-endian, not Twilio's wire-format mu-law.
    """

    session_id: str
    stream_id: str
    track: AudioTrack
    timestamp_ms: int
    pcm_s16le: bytes = field(repr=False)
    sample_rate: int = 8000
    channels: int = 1

    def __post_init__(self) -> None:
        if not self.session_id or not self.stream_id:
            raise ValueError("Audio frames require a session ID and stream ID.")
        if self.track not in ("inbound", "outbound"):
            raise ValueError("Audio track must be inbound or outbound.")
        if isinstance(self.timestamp_ms, bool) or not isinstance(self.timestamp_ms, int) or self.timestamp_ms < 0:
            raise ValueError("Frame timestamp must be a nonnegative integer in milliseconds.")
        if self.sample_rate != 8000 or self.channels != 1:
            raise ValueError("Build 2 audio is 8000 Hz mono PCM.")
        if not isinstance(self.pcm_s16le, bytes) or len(self.pcm_s16le) % 2:
            raise ValueError("PCM must be bytes containing whole 16-bit samples.")

    @property
    def sample_count(self) -> int:
        return len(self.pcm_s16le) // 2


@dataclass(frozen=True, slots=True)
class CaptureFinished:
    """End of a successful local replay, with its completed manifest reference."""

    session_id: str
    stream_id: str
    manifest_path: Path
    frame_count: int


class AudioConsumer(Protocol):
    """Implement these synchronous callbacks in a partner-owned module.

    The replay runner calls consumers sequentially. A consumer may perform
    local work or enqueue a frame; this protocol does not run inside live
    telephony callbacks and cannot change the call or play audio to callers.
    Exceptions stop replay and do not produce a successful end callback.
    """

    def on_frame(self, frame: AudioFrame) -> None: ...

    def on_end(self, capture: CaptureFinished) -> None: ...


class NullConsumer:
    """Default observer: retains no audio and calls no services."""

    def on_frame(self, frame: AudioFrame) -> None:
        pass

    def on_end(self, capture: CaptureFinished) -> None:
        pass
