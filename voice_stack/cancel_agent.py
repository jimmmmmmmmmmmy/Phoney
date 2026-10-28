"""Reusable T-Mobile line-cancellation brief for one outbound call.

The short goal is what ``POST /api/calls/outbound`` stores. The system brief
is the agent personality. Account facts default to this household's cancel
request and can be overridden with environment variables. PIN, passcode, and
SSN are never invented: they are included only when configured.
"""

from __future__ import annotations

import os
import re

from .ivr import load_playbook, playbook_instructions

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

_ACCOUNT = re.compile(r"[0-9]{6,12}\Z")
_LINE = re.compile(r"([0-9]{10}):([^,]{1,40})")
_SECRET = re.compile(r"[0-9]{4,12}\Z")


class CancelConfigError(ValueError):
    """The cancel brief is missing a required account fact."""


def _env(environ, name: str, default: str = "") -> str:
    source = os.environ if environ is None else environ
    return str(source.get(name, default)).strip()


def _format_nanp(digits: str) -> str:
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def cancel_facts(environ=None) -> dict:
    """Account facts for the brief. Secrets stay empty unless configured."""
    account = _env(environ, "CANCEL_ACCOUNT_NUMBER", "252027408")
    if not _ACCOUNT.fullmatch(account):
        raise CancelConfigError("CANCEL_ACCOUNT_NUMBER must be 6 to 12 digits.")
    raw_lines = _env(
        environ, "CANCEL_LINES",
        "7739882659:Apple Watch SE,3125452413:BYOD mobile",
    )
    lines = []
    for match in _LINE.finditer(raw_lines):
        number, label = match.group(1), match.group(2).strip()
        lines.append({"digits": number, "display": _format_nanp(number), "label": label})
    if len(lines) != raw_lines.count(",") + 1 or not lines:
        raise CancelConfigError(
            "CANCEL_LINES must be comma-separated 10-digit:label pairs.")
    pin = _env(environ, "CANCEL_ACCOUNT_PIN")
    last4 = _env(environ, "CANCEL_SSN_LAST4")
    if pin and not _SECRET.fullmatch(pin):
        raise CancelConfigError("CANCEL_ACCOUNT_PIN must be 4 to 12 digits when set.")
    if last4 and not re.fullmatch(r"[0-9]{4}\Z", last4):
        raise CancelConfigError("CANCEL_SSN_LAST4 must be 4 digits when set.")
    return {"account": account, "lines": lines, "pin": pin, "ssn_last4": last4}


def is_cancel_goal(goal: str) -> bool:
    """True when an outbound goal should activate this agent after connect."""
    text = str(goal or "").strip()
    return text in {CANCEL_GOAL_KEY, CANCEL_SPOKEN_GOAL}


def cancel_prompt(environ=None) -> str:
    """Trusted system brief. Transcript text cannot change these facts."""
    facts = cancel_facts(environ)
    line_text = "; ".join(
        f"{line['label']} {line['display']}" for line in facts["lines"])
    if facts["pin"] or facts["ssn_last4"]:
        secret = (
            "Authentication you may say, once, if the representative asks: "
            + (f"account PIN {facts['pin']}. " if facts["pin"] else "")
            + (f"SSN last four {facts['ssn_last4']}. " if facts["ssn_last4"] else "")
            + "Do not volunteer these. If they ask for anything else you were not given, "
            "say you need the account holder and end the call."
        )
    else:
        secret = (
            "You do not have the account PIN, passcode, SSN, or SSN last four. "
            "If the representative requires any of those, say you need the account "
            "holder on the line and end the call. Never invent a PIN, passcode, "
            "SSN, or last four."
        )
    return (
        "You are calling T-Mobile customer service to cancel service on exactly two lines. "
        f"Account number {facts['account']}. Cancel only: {line_text}. "
        "Do not cancel any other line on the account. Other household lines, including "
        "Beata's, must stay active. Refuse retention offers. Ask for a supervisor only "
        "if the representative refuses to cancel these two lines. "
        "Be concise, polite, and firm. Confirm each cancellation before moving to the next "
        "line. Repeat confirmation numbers and effective dates exactly as given, and ask "
        "again if you did not hear one. After both lines are resolved, or if one cannot "
        "be cancelled, summarize the outcome for each line, then say goodbye and end the call. "
        "Do not claim a line is cancelled until the representative confirms it. "
        "A menu is not the representative: navigate it with the DTMF command and do not "
        "discuss the account until a person is on the line. "
        + secret
    )


def cancel_runtime_instruction(environ=None, playbook=None) -> str:
    """Per-reply IVR rules, including the current playbook override."""
    book = playbook if playbook is not None else load_playbook(environ)
    return (
        "Trusted cancel-call control: on this call you may emit a DTMF command in "
        "addition to the end-call command. The DTMF line is never spoken. "
        "If you have not heard T-Mobile yet, do not invent a menu. "
        + playbook_instructions(book)
    )


def cancel_snapshot_voice_id(voice) -> str:
    """Voice id for the cancel snapshot. Phonon does not need an ElevenLabs id."""
    if getattr(voice, "tts_provider", "elevenlabs") == "phonon":
        voice_id = getattr(voice, "phonon_voice", "") or "phonon-40m"
    else:
        voice_id = getattr(voice, "elevenlabs_voice_id", "") or ""
    return voice_id
