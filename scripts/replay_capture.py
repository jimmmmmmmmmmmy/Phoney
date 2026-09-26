"""Replay a completed local capture. Default output is a metadata summary only."""

import argparse
import json
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integrations.example_observer import MetadataObserver
from integrations.replay import read_completed_capture, replay_capture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Path to a completed capture's manifest.json")
    parser.add_argument("--frames", action="store_true", help="Print metadata for each replay frame; never audio bytes")
    parser.add_argument("--realtime", action="store_true", help="Pace frames by recorded timestamps")
    parser.add_argument("--frame-ms", type=int, default=20, help="Replay frame duration from 10 to 1000 ms (default: 20)")
    args = parser.parse_args()
    capture = read_completed_capture(args.manifest)
    result = replay_capture(args.manifest, MetadataObserver() if args.frames else None,
                            frame_ms=args.frame_ms, realtime=args.realtime)
    print(json.dumps({"event": "capture_summary", "session_id": capture.session_id,
                      "stream_id": capture.stream_id, "duration_ms": capture.duration_ms,
                      "replayed_frames": result.frame_count,
                      "tracks": [{"track": track.name, "meaning": track.meaning, "samples": track.samples}
                                 for track in capture.tracks]}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, wave.Error, EOFError) as exc:
        print(f"Replay failed: {exc}", file=sys.stderr)
        sys.exit(1)
