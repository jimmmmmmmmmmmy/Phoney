"""Mission CLI for a coding agent. The default path does not dial."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx

from voice_stack.latency import report_lines, select_voice
from voice_stack.settings import VoiceSettings

from .bridge import facts_for
from .brief import assemble_brief
from .gate import live_call_block, unattended_dial_permitted
from .research import build_cache, default_cache_path, write_cache
from .schema import MissionSchemaError, load_pack

RUNS = Path(__file__).resolve().parent / "runs"


def _voice(environ):
    try:
        return VoiceSettings.from_env(environ=environ)
    except ValueError as exc:
        return exc


def prepare(mission: str, environ=None) -> str:
    pack = load_pack(mission)
    brief = assemble_brief(pack, environ)
    brief.assert_public_safe()
    return "\n".join([
        f"mission {pack.id} prepared (no call placed)",
        f"destination: {pack.destination_e164}",
        f"brevity: {pack.brevity}",
        f"tools: {', '.join(pack.tools)}",
        f"threshold_cents: {pack.retention.min_monthly_reduction_cents}",
        f"min_months: {pack.retention.min_months}",
        f"public_chars: {len(brief.public_text())}",
        "password: not loaded",
    ])


def research(mission: str, environ=None, dest: Path | None = None) -> str:
    pack = load_pack(mission)
    path = dest or default_cache_path(pack)
    write_cache(pack, path, environ)
    cache = build_cache(pack, environ)
    return "\n".join([
        f"research cached for {pack.id} (no call placed)",
        f"path: {path}",
        f"utterances: {len(cache['utterance_ids'])}",
        f"x_refresh_after: {cache['x_refresh_after'] or 'unset'}",
        "secrets: absent",
    ])


def check(mission: str, environ=None) -> str:
    env = os.environ if environ is None else environ
    pack = load_pack(mission)
    facts = facts_for(pack.goal_key, env)
    lines = ", ".join(f"{line['label']} {line['display']}" for line in facts["lines"])
    allow = [item.strip() for item in str(env.get("ALLOWED_DESTINATIONS", "")).split(",") if item.strip()]
    voice = _voice(env)
    if isinstance(voice, VoiceSettings):
        chosen = select_voice(voice, _settings_view(env))
        providers = (
            f"stt={chosen['stt']} tts={chosen['tts']} llm={chosen['llm']} "
            f"tts_fallback={chosen['tts_fallback'] or 'none'} "
            f"llm_fallback={chosen['llm_fallback'] or 'none'}"
        )
    else:
        providers = f"voice settings invalid: {voice}"
    attended, reason = unattended_dial_permitted(env, mission=True)
    return "\n".join([
        f"{pack.title} plan (no call placed)",
        f"destination: {pack.destination_e164}",
        f"goal: {pack.goal_key}",
        f"allowlisted: {str(pack.destination_e164 in allow).lower()}",
        f"account: {facts['account']}",
        f"lines: {lines}",
        f"pin configured: {str(bool(facts['pin'])).lower()}",
        f"ssn last4 configured: {str(bool(facts['ssn_last4'])).lower()}",
        "password: not loaded",
        f"brevity: {pack.brevity}",
        providers,
        f"owner gate: {'skipped' if attended else 'press 1 required'}",
        *([] if attended else [reason]),
        "Deepgram is the only STT implementation. A configured DEEPGRAM_API_KEY selects it.",
    ])


def _settings_view(env):
    class _Settings:
        deepgram_api_key = str(env.get("DEEPGRAM_API_KEY", ""))
        deepgram_model = str(env.get("DEEPGRAM_MODEL", "nova-3"))
        transcription_enabled = str(env.get("TRANSCRIPTION_ENABLED", "")).lower() == "true"
    return _Settings()


def place_call(mission: str, base: str, environ=None) -> dict:
    env = os.environ if environ is None else environ
    reason = live_call_block(base, env)
    if reason:
        raise SystemExit(reason)
    pack = load_pack(mission)
    token = str(env.get("OPERATOR_ADMIN_TOKEN", ""))
    if len(token) < 32:
        raise SystemExit("OPERATOR_ADMIN_TOKEN is missing.")
    response = httpx.post(
        base.rstrip("/") + "/api/calls/outbound",
        headers={"Authorization": "Bearer " + token, "Idempotency-Key": str(uuid.uuid4())},
        json={"to": pack.destination_e164, "goal": pack.goal_key},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    RUNS.mkdir(parents=True, exist_ok=True)
    record = {
        "mission": pack.id,
        "session_id": payload.get("session_id", ""),
        "phase": payload.get("phase", ""),
        "destination": pack.destination_e164,
        "unattended": unattended_dial_permitted(env, mission=True)[0],
    }
    (RUNS / f"{pack.id}-latest.json").write_text(json.dumps(record, indent=2) + "\n", "utf-8")
    return payload


def report(mission: str) -> str:
    path = RUNS / f"{mission}-latest.json"
    if not path.is_file():
        return f"No call has been placed for {mission}."
    data = json.loads(path.read_text("utf-8"))
    return json.dumps({key: data.get(key) for key in ("mission", "session_id", "phase", "destination", "unattended")}, indent=2)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or run a Phoney phone mission")
    parser.add_argument("command", choices=("prep", "research", "check", "place-call", "report", "run"))
    parser.add_argument("mission")
    parser.add_argument("--base", default=os.environ.get("PHONEY_LOCAL_BASE", "http://127.0.0.1:8000"))
    parser.add_argument("--simulate", action="store_true",
                        help="Run the artificial call with a fake dialer. Does not contact Twilio.")
    args = parser.parse_args(argv)
    try:
        if args.simulate:
            from .artificial import run_artificial_call
            result = asyncio.run(run_artificial_call(args.mission))
            print(result.summary())
            if not (result.unattended and result.brief_has_cancel_facts and not result.owner_dialed
                    and not result.press_1 and result.dtmf_updates >= 1 and result.calls_created == 1
                    and not any(host not in {"127.0.0.1", "localhost"} for host in result.hosts)):
                print("artificial call did not meet the mission checks", file=sys.stderr)
                return 1
            return 0
        if args.command == "prep":
            print(prepare(args.mission))
            return 0
        if args.command == "research":
            print(research(args.mission))
            return 0
        if args.command == "check":
            print(check(args.mission))
            return 0
        if args.command == "report":
            print(report(args.mission))
            return 0
        if args.command == "run":
            print(prepare(args.mission))
            print(research(args.mission))
            print(check(args.mission))
            reason = live_call_block(args.base)
            if reason:
                print("did not dial: " + reason)
                return 0
            print(json.dumps(place_call(args.mission, args.base), indent=2))
            return 0
        reason = live_call_block(args.base)
        if reason:
            print(reason, file=sys.stderr)
            return 2
        print(json.dumps(place_call(args.mission, args.base), indent=2))
        return 0
    except MissionSchemaError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SystemExit as exc:
        if exc.code is None or isinstance(exc.code, int):
            raise
        print(str(exc.code), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
