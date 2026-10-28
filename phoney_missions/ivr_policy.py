"""DTMF policy: auto only when the recording just offered that digit.

Account-cancel and whole-account menus stay model-only, and a wipe phrase
blocks digits entirely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from voice_stack.ivr import PlaybookEntry, suggest_dtmf

from .schema import MissionPack

_WIPE = re.compile(
    r"all lines|entire account|whole account|cancel all|every line|close the account|close my account",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DtmfDecision:
    mode: str
    digits: str


def playbook_entries(pack: MissionPack) -> tuple[PlaybookEntry, ...]:
    return tuple(
        PlaybookEntry(entry.id, entry.phrases, auto=entry.auto, digits=entry.digits, priority=entry.priority)
        for entry in pack.ivr
    )


def account_wipe(transcript: str) -> bool:
    return bool(isinstance(transcript, str) and _WIPE.search(transcript))


def classify_dtmf(transcript: str, playbook: tuple[PlaybookEntry, ...]) -> DtmfDecision:
    """``auto`` sends now. ``model-only`` and ``blocked`` send nothing."""
    if account_wipe(transcript):
        return DtmfDecision("blocked", "")
    digits = suggest_dtmf(transcript, playbook)
    if digits:
        return DtmfDecision("auto", digits)
    folded = " ".join(str(transcript or "").lower().split())
    for entry in playbook:
        if entry.auto:
            continue
        if any(phrase.lower() in folded for phrase in entry.phrases):
            return DtmfDecision("model-only", "")
    return DtmfDecision("none", "")
