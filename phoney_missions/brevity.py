"""Strict spoken-turn budget for phone missions.

One short sentence is the default. Confirming a number or a date may use a
second short sentence. Filler is a policy failure, not a style preference.
"""

from __future__ import annotations

import re

_SENTENCE = re.compile(r"[^.!?]+[.!?]?")
_CONFIRM = re.compile(
    r"\d{4,}|20\d{2}-\d{2}-\d{2}|\b(?:confirmation|effective)\b",
    re.IGNORECASE,
)

# Tight on purpose. A test fails when a sample reply is longer than this.
DEFAULT_MAX_WORDS = 18
DEFAULT_MAX_CHARS = 110
DEFAULT_MAX_SENTENCES = 1
CONFIRM_MAX_WORDS = 32
CONFIRM_MAX_CHARS = 180
CONFIRM_MAX_SENTENCES = 2

BANS = (
    "No filler.",
    "No empathy padding.",
    "Do not restate the question.",
    "Do not ramble.",
    "When a menu digit works, send DTMF and do not speak.",
)


def sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.findall(text or "") if part.strip()]


def is_confirmation(text: str) -> bool:
    return bool(_CONFIRM.search(text or ""))


def limits(kind: str) -> tuple[int, int, int]:
    if kind == "confirmation":
        return CONFIRM_MAX_WORDS, CONFIRM_MAX_CHARS, CONFIRM_MAX_SENTENCES
    return DEFAULT_MAX_WORDS, DEFAULT_MAX_CHARS, DEFAULT_MAX_SENTENCES


def kind_for(text: str, kind: str | None = None) -> str:
    if kind in {"default", "confirmation"}:
        return kind
    return "confirmation" if is_confirmation(text) else "default"


def exceeds_budget(text: str, kind: str | None = None) -> bool:
    """True when this spoken utterance is too long for the strict policy."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return False
    words, chars, sent_max = limits(kind_for(cleaned, kind))
    return (len(cleaned.split()) > words or len(cleaned) > chars
            or len(sentences(cleaned)) > sent_max)


def budget_label(text: str, kind: str | None = None) -> str:
    words, chars, sent_max = limits(kind_for(text, kind))
    return f"max {sent_max} sentence(s), {words} words, {chars} characters"


def assert_spoken_budget(text: str, kind: str | None = None) -> None:
    """Fail the test when a sample reply is over the strict budget."""
    if exceeds_budget(text, kind):
        shown = " ".join(str(text).split())[:80]
        raise AssertionError(f"spoken reply exceeds brevity budget ({budget_label(text, kind)}): {shown}")


def fit_spoken_phrase(text: str) -> str:
    """Shorten one TTS phrase. Confirmation numbers are kept when they fit."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned or not exceeds_budget(cleaned):
        return cleaned
    first = sentences(cleaned)[0]
    if not exceeds_budget(first):
        return first
    words, chars, _sent = limits(kind_for(first))
    clipped = " ".join(first.split()[:words])
    if len(clipped) > chars:
        clipped = clipped[:chars].rsplit(" ", 1)[0]
    return clipped.strip()


def instruction() -> str:
    """Trusted brevity rules. Appended to every strict mission brief."""
    return (
        "Brevity policy strict: each spoken reply is one short sentence, or a few words. "
        + " ".join(BANS)
        + " Use a second short sentence only to confirm a number or a date. "
        "Say at most one allowed line per turn."
    )
