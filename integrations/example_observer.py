"""Example local observer. Prints metadata only; never prints audio payloads."""

import json

from .contracts import AudioFrame, CaptureFinished


class MetadataObserver:
    def __init__(self) -> None:
        self.sequence = 0

    def on_frame(self, frame: AudioFrame) -> None:
        self.sequence += 1
        print(json.dumps({"event": "frame", "sequence": self.sequence,
                          "track": frame.track, "timestamp_ms": frame.timestamp_ms,
                          "samples": frame.sample_count, "bytes": len(frame.pcm_s16le)}))

    def on_end(self, capture: CaptureFinished) -> None:
        print(json.dumps({"event": "replay_completed", "frames": capture.frame_count}))
