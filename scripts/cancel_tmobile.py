#!/usr/bin/env python3
"""Plan or start the T-Mobile cancel call through a local Phoney server.

The default is a dry run. ``--place-call`` posts to loopback Phoney only, and
only when ``PHONEY_ALLOW_LIVE_CALL=1``. It never talks to Twilio directly, so a
cloud agent without that flag cannot dial.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_stack.cancel_agent import (CANCEL_GOAL_KEY, TMOBILE_CS_E164,  # noqa: E402
                                     TMOBILE_IN_NETWORK_SHORT_CODE, cancel_facts, cancel_prompt)
from voice_stack.settings import VoiceSettings  # noqa: E402


def live_call_block(base: str, environ=None) -> str | None:
    """Return a refusal reason, or None when a loopback dial may be posted."""
    env = os.environ if environ is None else environ
    if env.get("CI") or env.get("GITHUB_ACTIONS"):
        return "Refusing to place a call in CI."
    if env.get("PHONEY_ALLOW_LIVE_CALL") != "1":
        return "Set PHONEY_ALLOW_LIVE_CALL=1 on the Mac that should dial."
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return "Refusing to dial through a non-loopback URL."
    return None


def describe_plan(environ=None) -> str:
    """Text plan. This does not reserve a session or place a call."""
    env = os.environ if environ is None else environ
    try:
        voice = VoiceSettings.from_env(environ=env)
        providers = (
            f"tts={voice.tts_provider} llm={voice.llm_provider} "
            f"enabled={str(voice.enabled).lower()}"
        )
        if voice.tts_provider == "phonon":
            providers += f" phonon={voice.phonon_base_url or '(unset)'}"
        if voice.llm_provider == "nemotron":
            providers += f" nemotron={voice.nemotron_base_url or '(unset)'}"
    except ValueError as exc:
        providers = f"voice settings invalid: {exc}"
    facts = cancel_facts(environ=env)
    lines = ", ".join(f"{line['label']} {line['display']}" for line in facts["lines"])
    allow = [item.strip() for item in str(env.get("ALLOWED_DESTINATIONS", "")).split(",") if item.strip()]
    listed = TMOBILE_CS_E164 in allow
    paths = []
    for label, name in (("phonon", "PHONON_MODEL_PATH"), ("nemotron", "NEMOTRON_MODEL_PATH")):
        raw = str(env.get(name, "")).strip()
        if not raw:
            paths.append(f"{label} model path unset")
        else:
            paths.append(f"{label} model path {'found' if Path(raw).exists() else 'missing'}: {raw}")
    prompt = cancel_prompt(environ=env)
    return "\n".join([
        "T-Mobile cancel plan (no call placed)",
        f"destination: {TMOBILE_CS_E164}",
        f"in-network short code {TMOBILE_IN_NETWORK_SHORT_CODE} is not dialable from Twilio",
        f"allowlisted: {str(listed).lower()}",
        f"goal: {CANCEL_GOAL_KEY}",
        f"account: {facts['account']}",
        f"lines: {lines}",
        f"pin configured: {str(bool(facts['pin'])).lower()}",
        f"ssn last4 configured: {str(bool(facts['ssn_last4'])).lower()}",
        providers,
        *paths,
        f"brief characters: {len(prompt)}",
        "Next: start local Phonon and Nemotron, then run with --place-call on the Mac.",
    ])


def place_call(base: str, token: str) -> dict:
    response = httpx.post(
        base.rstrip("/") + "/api/calls/outbound",
        headers={"Authorization": "Bearer " + token, "Idempotency-Key": str(uuid.uuid4())},
        json={"to": TMOBILE_CS_E164, "goal": CANCEL_GOAL_KEY},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Plan or start the T-Mobile cancel call")
    parser.add_argument("--check", action="store_true", help="Print the plan and exit")
    parser.add_argument("--place-call", action="store_true", help="POST the outbound call to local Phoney")
    parser.add_argument("--base", default=os.environ.get("PHONEY_LOCAL_BASE", "http://127.0.0.1:8000"))
    args = parser.parse_args(argv)
    if args.place_call:
        reason = live_call_block(args.base)
        if reason:
            print(reason, file=sys.stderr)
            return 2
        token = os.environ.get("OPERATOR_ADMIN_TOKEN", "")
        if len(token) < 32:
            print("OPERATOR_ADMIN_TOKEN is missing.", file=sys.stderr)
            return 2
        print(json.dumps(place_call(args.base, token), indent=2))
        return 0
    print(describe_plan())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
