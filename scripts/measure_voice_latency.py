#!/usr/bin/env python3
"""Print STT, LLM, and TTS budgets. This script does not dial.

On the Mac, point it at the same private env Phoney loads. It reports whether
Deepgram, Phonon, and Nemotron are selected. It never prints key values.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice_stack.latency import report_lines  # noqa: E402
from voice_stack.settings import VoiceSettings  # noqa: E402


class _Stt:
    def __init__(self, env):
        self.deepgram_api_key = str(env.get("DEEPGRAM_API_KEY", ""))
        self.deepgram_model = str(env.get("DEEPGRAM_MODEL", "nova-3"))
        self.transcription_enabled = str(env.get("TRANSCRIPTION_ENABLED", "")).lower() == "true"


def main() -> int:
    if "--place-call" in sys.argv:
        print("This script cannot place a call.", file=sys.stderr)
        return 2
    try:
        voice = VoiceSettings.from_env()
    except ValueError as exc:
        print(f"voice settings invalid: {exc}", file=sys.stderr)
        return 2
    for line in report_lines(voice, _Stt(os.environ)):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
