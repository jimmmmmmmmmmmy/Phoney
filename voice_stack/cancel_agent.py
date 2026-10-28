"""Reusable T-Mobile line-cancellation brief for one outbound call.

The short goal is what ``POST /api/calls/outbound`` stores. The system brief
is the agent personality. Account facts default to this household's cancel
request and can be overridden with environment variables. PIN, passcode, and
SSN are never invented: they are included only when configured.
"""

from __future__ import annotations

import os
import re

TMOBILE_CS_E164 = "+18009378997"
TMOBILE_CS_DISPLAY = "1-800-937-8997"
# 611 reaches T-Mobile only from a T-Mobile handset. Twilio cannot dial it.
TMOBILE_IN_NETWORK_SHORT_CODE = "611"
CANCEL_GOAL_KEY = "cancel-tmobile"
CANCEL_AGENT_KIND = "cancel-tmobile"
CANCEL_PROFILE = "cancel"
CANCEL_SPOKEN_GOAL = (
    "Cancel only the two named T-Mobile lines, confirm each one, and leave every other line active."
)

_SECRET = re.compile(r"[0-9]{4,12}\Z")


class CancelConfigError(ValueError):
    """The cancel brief is missing a required account fact."""


def _env(environ, name: str, default: str = "") -> str:
    source = os.environ if environ is None else environ
    return str(source.get(name, default)).strip()


def cancel_facts(environ=None) -> dict:
    """Account facts for the brief. Secrets stay empty unless configured."""
    from phoney_missions.bridge import facts_for
    from phoney_missions.schema import MissionSchemaError
    try:
        facts = facts_for(CANCEL_GOAL_KEY, environ)
    except MissionSchemaError as exc:
        raise CancelConfigError(str(exc)) from exc
    pin = _env(environ, "CANCEL_ACCOUNT_PIN")
    last4 = _env(environ, "CANCEL_SSN_LAST4")
    if pin and not _SECRET.fullmatch(pin):
        raise CancelConfigError("CANCEL_ACCOUNT_PIN must be 4 to 12 digits when set.")
    if last4 and not re.fullmatch(r"[0-9]{4}\Z", last4):
        raise CancelConfigError("CANCEL_SSN_LAST4 must be 4 digits when set.")
    facts["pin"] = pin
    facts["ssn_last4"] = last4
    return facts


def is_cancel_goal(goal: str) -> bool:
    """True when an outbound goal should activate this agent after connect."""
    text = str(goal or "").strip()
    return text in {CANCEL_GOAL_KEY, CANCEL_SPOKEN_GOAL}


def cancel_prompt(environ=None) -> str:
    """Public system brief. PIN and SSN live in the runtime instruction, not here."""
    from phoney_missions.bridge import public_prompt_for
    return public_prompt_for(CANCEL_GOAL_KEY, environ)


def cancel_runtime_instruction(environ=None, playbook=None) -> str:
    """Per-reply IVR rules, policy, brevity, and configured authentication."""
    from phoney_missions.bridge import runtime_instruction_for
    return runtime_instruction_for(CANCEL_GOAL_KEY, playbook=playbook, environ=environ)


def cancel_snapshot_voice_id(voice) -> str:
    """Voice id for the cancel snapshot. Phonon does not need an ElevenLabs id."""
    if getattr(voice, "tts_provider", "elevenlabs") == "phonon":
        voice_id = getattr(voice, "phonon_voice", "") or "phonon-40m"
    else:
        voice_id = getattr(voice, "elevenlabs_voice_id", "") or ""
    return voice_id
