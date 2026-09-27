"""Explicit offline Modulate replay experiment. It never changes live call handling."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from integrations.replay import MAX_MANIFEST_BYTES, iter_audio_frames, read_completed_capture
from partner_detection.modulate import MAX_AUDIO_SECONDS, stream_inbound_pcm


async def replay_inbound(capture, *, paced: bool):
    started = time.monotonic()
    for frame in iter_audio_frames(capture):
        if frame.track != "inbound":
            continue
        if paced:
            remaining = started + frame.timestamp_ms / 1000 - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
        yield frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="completed local capture manifest")
    parser.add_argument("--send-to-provider", action="store_true",
                        help="explicitly allow sending inbound audio to Modulate")
    parser.add_argument("--env-file", type=Path, help="environment file; defaults to project .env")
    parser.add_argument("--paced", action="store_true", help="pace replay by capture timestamps")
    parser.add_argument("--deadline-seconds", type=float)
    parser.add_argument("--max-audio-seconds", type=int,
                        help="1–120 local limit; conservative and provisional")
    args = parser.parse_args()
    capture = read_completed_capture(args.manifest)
    inbound = next(track for track in capture.tracks if track.name == "inbound")
    if not args.send_to_provider:
        print(json.dumps({"event": "detection_dry_run", "stage": "offline_only", "network": False,
                          "session_id": capture.session_id, "stream_id": capture.stream_id,
                          "track": "inbound", "inbound_duration_ms": inbound.samples // 8,
                          "reason": "pass --send-to-provider to transmit audio"}))
        return 0
    # Replay WAVs contain padding. Do not treat missing captured samples as speech evidence.
    with capture.manifest_path.open("rb") as source:
        raw = source.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("capture manifest exceeds its metadata limit")
    manifest = json.loads(raw)
    counters = manifest.get("counters", {})
    quality_values = [manifest.get("tracks", {}).get("inbound", {}).get("gap_samples"),
                      counters.get("dropped_messages"), counters.get("rejected_messages")]
    if any(type(value) is not int or value != 0 for value in quality_values):
        print(json.dumps({"event": "detection_result", "stage": "offline_only", "network": False,
                          "status": "unknown", "reason": "incomplete_audio",
                          "session_id": capture.session_id, "stream_id": capture.stream_id}))
        return 2
    load_dotenv(args.env_file or ROOT / ".env", override=bool(args.env_file))
    deadline_seconds = (args.deadline_seconds if args.deadline_seconds is not None else
                        float(os.environ.get("MODULATE_DETECTION_DEADLINE_SECONDS", "45")))
    max_audio_seconds = (args.max_audio_seconds if args.max_audio_seconds is not None else
                         int(os.environ.get("MODULATE_DETECTION_MAX_AUDIO_SECONDS", str(MAX_AUDIO_SECONDS))))
    if not 1 <= max_audio_seconds <= MAX_AUDIO_SECONDS:
        raise ValueError("--max-audio-seconds must be from 1 through 120")
    if inbound.samples > max_audio_seconds * 8000:
        print(json.dumps({"event": "detection_result", "stage": "offline_only", "network": False,
                          "status": "unknown", "reason": "audio_limit_exceeded",
                          "session_id": capture.session_id, "stream_id": capture.stream_id}))
        return 2
    key = os.environ.get("MODULATE_API_KEY", "")
    report = asyncio.run(stream_inbound_pcm(replay_inbound(capture, paced=args.paced), api_key=key,
                                            deadline_seconds=deadline_seconds,
                                            collection_seconds=max_audio_seconds + 15 if args.paced else deadline_seconds,
                                            max_audio_seconds=max_audio_seconds))
    output = report.to_dict() | {"event": "detection_result", "stage": "offline_only",
                                 "paced": args.paced, "network": True}
    print(json.dumps(output, sort_keys=True))
    return 0 if report.reason is None else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"Detection failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
