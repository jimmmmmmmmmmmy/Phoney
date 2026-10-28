#!/usr/bin/env python3
"""Run a phone mission: prep, research, check, place-call, report.

``place-call`` posts to loopback Phoney only. It does not talk to Twilio.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phoney_missions.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
