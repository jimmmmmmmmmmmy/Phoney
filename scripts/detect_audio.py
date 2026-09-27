"""Explicit local WAV/MP3 batch check against Modulate; dry-run by default."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from partner_detection.modulate import MAX_BATCH_AUDIO_BYTES, ModulateError, detect_audio


def emit(result: dict[str, object], output: Path | None) -> None:
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if output is None:
        print(encoded, end="")
        return
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as destination:
        os.fchmod(destination.fileno(), 0o600)
        destination.write(encoded)
    print(str(output))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, help="local .wav or .mp3 file")
    parser.add_argument("--send-to-provider", action="store_true",
                        help="explicitly allow uploading this file to Modulate")
    parser.add_argument("--env-file", type=Path, help="environment file; defaults to project .env")
    parser.add_argument("--session-id", default="", help="result correlation ID; defaults to filename stem")
    parser.add_argument("--track", choices=("inbound", "outbound"), default="inbound")
    parser.add_argument("--source-start-ms", type=int, default=0)
    parser.add_argument("--output", type=Path, help="write the JSON result to this file")
    args = parser.parse_args()

    audio_path = args.audio.expanduser().resolve(strict=True)
    if not audio_path.is_file() or audio_path.suffix.lower() not in {".wav", ".mp3"}:
        raise ValueError("audio must be a regular .wav or .mp3 file")
    if audio_path.stat().st_size > MAX_BATCH_AUDIO_BYTES:
        raise ValueError("audio exceeds the 100 MiB local limit")

    if not args.send_to_provider:
        emit({"event": "batch_detection_dry_run", "file": str(audio_path),
              "network": False, "reason": "pass --send-to-provider to transmit audio",
              "stage": "offline_only"}, args.output)
        return 0

    load_dotenv(args.env_file or ROOT / ".env", override=bool(args.env_file))
    with audio_path.open("rb") as source:
        audio_bytes = source.read(MAX_BATCH_AUDIO_BYTES + 1)
    if len(audio_bytes) > MAX_BATCH_AUDIO_BYTES:
        raise ValueError("audio exceeds the 100 MiB local limit")
    result = detect_audio(audio_bytes, filename=audio_path.name,
                          api_key=os.environ.get("MODULATE_API_KEY", ""),
                          session_id=args.session_id or audio_path.stem,
                          track=args.track, source_start_ms=args.source_start_ms)
    emit(result | {"event": "batch_detection_result", "file": str(audio_path),
                   "network": True, "stage": "offline_only"}, args.output)
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, ModulateError) as exc:
        print(f"Detection failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
