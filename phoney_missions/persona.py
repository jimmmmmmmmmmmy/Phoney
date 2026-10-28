"""The caller is the account holder. Spoken text must not break that cover."""

from __future__ import annotations

import re

# Short tokens use a word boundary so "said" and "again" are not treated as "ai".
_REVEAL = (
    re.compile(r"\bai\b", re.IGNORECASE),
    re.compile(r"artificial", re.IGNORECASE),
    re.compile(r"language model", re.IGNORECASE),
    re.compile(r"\bllm\b", re.IGNORECASE),
    re.compile(r"\bbot\b", re.IGNORECASE),
    re.compile(r"automated", re.IGNORECASE),
    re.compile(r"assistant", re.IGNORECASE),
    re.compile(r"\bcomputer\b", re.IGNORECASE),
    re.compile(r"\bscript\b", re.IGNORECASE),
    re.compile(r"recording this for training", re.IGNORECASE),
    re.compile(r"not a real person", re.IGNORECASE),
    re.compile(r"account holder on the line", re.IGNORECASE),
    re.compile(r"on behalf of", re.IGNORECASE),
    re.compile(r"\bmy client\b", re.IGNORECASE),
)


def reveals(text: str) -> bool:
    return any(pattern.search(str(text or "")) for pattern in _REVEAL)


def hold_cover(text: str, fallback: str) -> str:
    """Replace a line that would admit automation. The fallback is a canned cancel."""
    cleaned = " ".join(str(text or "").split())
    if not cleaned or not reveals(cleaned):
        return cleaned
    safe = " ".join(str(fallback or "Please cancel that line.").split())
    return safe if not reveals(safe) else "Please cancel that line."


def persona_rules(holder: str) -> str:
    return (
        f"You are {holder}, the person who owns this account. "
        "Speak in the first person as yourself. "
        "Never say you are an assistant, a bot, automated, a recording, or a language model. "
        "Never say you are calling for someone else, and never ask for the account holder. "
        "If you cannot answer a verification request, say you will call back and end the call."
    )
