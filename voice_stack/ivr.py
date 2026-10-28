"""IVR digits and a small overridable T-Mobile menu playbook.

Twilio ``<Play digits>`` accepts ``0-9``, ``A-D``, ``*``, ``#``, ``w`` (half a
second), and ``W`` (one second). Menus change. The playbook only auto-sends a
digit when the caller just heard that digit next to a navigation intent.
Cancel choices stay with the model so a whole-account menu is not pressed by a
guess.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("uvicorn.error")

DTMF_CHARS = frozenset("0123456789ABCD*#wW")
MAX_DTMF_CHARS = 32
DTMF_LINE = re.compile(r"\[/DTMF ([0-9A-D*#wW]{1,32})\]\Z")
NOTE_LINE = re.compile(r"\[/NOTE ([A-Za-z0-9:._-]{1,160})\]\Z")

_PRESS = re.compile(
    r"\bpress\s+(?:the\s+)?(\d|star|pound|zero|oh|one|two|three|four|five|six|seven|eight|nine)\b"
    r"([^.!?\n]{0,80})",
    re.IGNORECASE,
)
_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "star": "*", "pound": "#",
}


def validate_dtmf(digits: str) -> str:
    """Return digits Twilio can play, or raise ``ValueError``."""
    if not isinstance(digits, str):
        raise ValueError("DTMF digits must be text.")
    text = digits.strip()
    if not text or len(text) > MAX_DTMF_CHARS or any(char not in DTMF_CHARS for char in text):
        raise ValueError("DTMF digits must be Twilio play digits, at most 32 characters.")
    return text


@dataclass(frozen=True)
class PlaybookEntry:
    """One menu hint. ``digits`` set means send those keys when every phrase is heard.

    Entries without ``digits`` match a spoken "press N" clause. Only ``auto``
    entries send without a model turn.
    """

    id: str
    phrases: tuple[str, ...]
    auto: bool = False
    digits: str = ""
    priority: int = 10

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Playbook entry needs an id.")
        if not self.phrases or any(not isinstance(phrase, str) or not phrase.strip() for phrase in self.phrases):
            raise ValueError(f"Playbook entry {self.id} needs phrases.")
        if self.digits:
            validate_dtmf(self.digits)
        if type(self.auto) is not bool:
            raise ValueError(f"Playbook entry {self.id} auto flag must be true or false.")
        if type(self.priority) is not int or isinstance(self.priority, bool):
            raise ValueError(f"Playbook entry {self.id} priority must be an integer.")


def default_playbook() -> tuple[PlaybookEntry, ...]:
    """Hints for T-Mobile customer service. Confirm the prompt before relying on them.

    ``representative`` is the only default auto-send: the digit still has to be
    one the recording just said. Account and cancel choices are model-only so a
    changed menu cannot drop every line on the account.
    """
    return (
        PlaybookEntry(
            "representative",
            ("representative", "customer care", "speak to an agent",
             "speak with a representative", "speak to a representative", "operator"),
            auto=True, priority=0,
        ),
        PlaybookEntry(
            "account-changes",
            ("account change", "account changes", "change a line", "manage line",
             "manage your line", "make a change"),
            auto=False, priority=1,
        ),
        PlaybookEntry(
            "cancel-line",
            ("cancel a line", "cancel the line", "disconnect a line", "cancel service",
             "disconnect service"),
            auto=False, priority=2,
        ),
    )


def playbook_instructions(playbook: tuple[PlaybookEntry, ...] | None = None) -> str:
    """Trusted text for the model. This is not spoken."""
    entries = playbook if playbook is not None else default_playbook()
    lines = [
        "IVR playbook: menus change. Press a digit only when this recording just "
        "offered it, or when an override entry below matches. Never invent a menu "
        "number. If the choice would cancel the whole account or any line other "
        "than the two named lines, do not press it; ask for a representative.",
        "When the other party is a menu, reply with only a [/DTMF digits] line and "
        "no spoken words. Digits use 0-9, A-D, *, #, w for a short pause, and W "
        "for a longer pause, at most 32 characters.",
    ]
    for entry in entries:
        send = f" override digits {entry.digits}" if entry.digits else " use the digit the menu just spoke"
        mode = "auto when the spoken digit matches" if entry.auto and not entry.digits else (
            "auto when every phrase is heard" if entry.auto else "model decides")
        lines.append(
            f"- {entry.id} ({mode}): phrases {', '.join(entry.phrases)};{send}."
        )
    return "\n".join(lines)


def load_playbook(environ=None) -> tuple[PlaybookEntry, ...]:
    """Load ``TMOBILE_IVR_PLAYBOOK`` JSON, or the default when it is unset.

    A missing or invalid file logs and returns the default so a live call is
    not stuck on a bad path.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get("TMOBILE_IVR_PLAYBOOK", "")).strip()
    if not raw:
        return default_playbook()
    try:
        document = json.loads(Path(raw).read_text("utf-8"))
        rows = document.get("entries") if isinstance(document, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ValueError("entries must be a non-empty list")
        loaded = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("entry must be an object")
            phrases = row.get("phrases")
            if not isinstance(phrases, list):
                raise ValueError("phrases must be a list")
            loaded.append(PlaybookEntry(
                str(row.get("id", "")),
                tuple(str(phrase) for phrase in phrases),
                auto=bool(row.get("auto", False)),
                digits=str(row.get("digits", "")),
                priority=int(row.get("priority", 10)),
            ))
        return tuple(loaded)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log.warning("ivr_playbook_unavailable reason=%s", type(exc).__name__)
        return default_playbook()


def _digit(token: str) -> str:
    lowered = token.lower()
    if lowered in _WORDS:
        return _WORDS[lowered]
    if len(token) == 1 and token in DTMF_CHARS:
        return token
    raise ValueError("not a menu digit")


def suggest_dtmf(transcript: str, playbook: tuple[PlaybookEntry, ...] | None = None) -> str | None:
    """Return digits to send for this utterance, or None when the model should decide.

    Explicit override entries win when every phrase occurs. Otherwise an auto
    entry matches only a "press N ..." clause that contains one of its phrases.
    """
    if not isinstance(transcript, str) or not transcript.strip():
        return None
    entries = playbook if playbook is not None else default_playbook()
    folded = " ".join(transcript.lower().split())
    explicit = []
    for entry in entries:
        if entry.auto and entry.digits and all(phrase.lower() in folded for phrase in entry.phrases):
            explicit.append((entry.priority, entry.digits))
    if explicit:
        explicit.sort()
        return explicit[0][1]
    found = []
    for match in _PRESS.finditer(transcript):
        try:
            digit = _digit(match.group(1))
        except ValueError:
            continue
        clause = match.group(2).lower()
        for entry in entries:
            if not entry.auto or entry.digits:
                continue
            if any(phrase.lower() in clause for phrase in entry.phrases):
                found.append((entry.priority, match.start(), digit))
                break
    if not found:
        return None
    found.sort()
    return found[0][2]
