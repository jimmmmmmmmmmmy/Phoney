"""Enroll the owner's voice once and print the ID to store in ``.env``.

Sample audio stays on this machine; only its bytes are uploaded, and the key is
never printed. The returned ``voice_id`` is reused by every call, so this runs
once per voice rather than inside a media handler.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_stack.settings import VoiceSettings
from voice_stack.tts import TTSError, create_clone


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True,
                        help="Explicit environment file holding ELEVENLABS_API_KEY")
    parser.add_argument("--name", required=True, help="Name for the enrolled voice")
    parser.add_argument("samples", nargs="+", type=Path,
                        help="Recordings of the owner: about 1-2 minutes of clean speech")
    args = parser.parse_args()

    settings = VoiceSettings.from_env(args.env_file)
    if not settings.elevenlabs_api_key:
        raise ValueError("Set ELEVENLABS_API_KEY in the supplied environment file.")
    result = create_clone(settings.elevenlabs_api_key, args.name, args.samples)
    voice_id = result["voice_id"]
    needs_verification = bool(result.get("requires_verification", False))
    print(json.dumps({
        "voice_id": voice_id,
        "name": args.name,
        "requires_verification": needs_verification,
        "samples": [Path(path).name for path in args.samples],
        "env_line": f"ELEVENLABS_VOICE_ID={voice_id}",
    }, indent=2))
    if needs_verification:
        print("This voice requires provider verification before it can speak. "
              "Complete it in the ElevenLabs dashboard, then rerun voice_check.py.",
              file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, TTSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)