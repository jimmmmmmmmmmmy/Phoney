"""Short voicemail acknowledgements when Gemini fails before speaking.

Recovery does not fabricate a summary or repeat caller text. The returned phase
keeps an acknowledgement separate from an actual summary confirmation, and the
runtime applies any end request only after the spoken reply has played.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from .prompts import VOICEMAIL_GREETING

MAX_RECOVERY_REPLY_CHARS = 180
_PHASES = frozenset({"greeting", "capture", "readback", "confirm", "followup",
                     "complete", "no_message", "unconfirmed", "followup_timeout"})
_CONTROL_MARKERS = re.compile(
    r"\[\s*/?\s*(?:end[\s_]+call|hang[\s_]*up)\s*\]|<\|[^<>]*\|>", re.I)
_YES = frozenset({
    "yes", "yep", "yup", "correct", "right", "exactly", "that's correct",
    "that is correct", "that's right", "that is right", "that's it",
    "yes that's correct", "yes that is correct", "yes that's right",
    "yes that is right", "yes correct", "yes exactly",
})
_NO = frozenset({
    "no", "nope", "nah", "incorrect", "not correct", "that's wrong",
    "that is wrong", "that's not correct", "that is not correct",
    "no that's wrong", "no that is wrong", "no that's not correct",
    "no that is not correct", "no not correct", "no incorrect",
})
_FOLLOWUP_DONE = frozenset({
    "no", "nope", "nah", "nothing else", "nothing more", "no nothing else",
    "no nothing more", "no thank you", "no thanks", "that's all", "that is all",
    "that's it", "that is it", "no that's all", "no that is all",
    "no that's it", "no that is it", "nope that's all", "nope that is all",
})
_FOLLOWUP_MORE = frozenset({"yes", "yep", "yup", "yes please"})
_UNCLEAR = frozenset({
    "", "maybe", "not sure", "i'm not sure", "i am not sure", "i don't know",
    "i do not know", "i think so", "what", "huh", "pardon", "sorry", "okay",
    "ok", "hello", "are you there", "can you repeat that", "please repeat that",
    "could you repeat that", "say that again", "what do you mean",
    "i didn't understand", "i did not understand",
})

_FOLLOWUP_QUESTION = "Is there anything else you'd like to add?"
_FAREWELL = "Thank you for your message. Goodbye."


@dataclass(frozen=True)
class RecoveryReply:
    spoken: str
    end_requested: bool
    phase: str


def _latest_caller_turn(context: Sequence[tuple[str, str]]) -> str:
    """Keep split final transcript chunks, stopping at the last agent reply."""
    parts = []
    for speaker, text in reversed(context):
        if speaker == "agent":
            break
        if speaker == "remote" and text.strip():
            # Preserve original controls until classification; stripping here
            # could turn a control-bearing answer into an ordinary yes or no.
            parts.append(text)
    return " ".join(reversed(parts))


def _normalized_reply(text: str) -> str:
    return " ".join(re.sub(r"[^\w']+", " ", text.replace("’", "'").casefold()).split())


def _contains_control(text: str) -> bool:
    return bool(_CONTROL_MARKERS.search(text)) or any(
        unicodedata.category(char) in {"Cc", "Cf"} and char not in "\t\n\r"
        for char in text
    )


def _has_speech(text: str) -> bool:
    text = _CONTROL_MARKERS.sub(" ", text)
    return any(char.isalnum() for char in text)


def recovery_reply(phase: str, context: Sequence[tuple[str, str]], *,
                   caller_requested_end: bool = False) -> RecoveryReply:
    """Return fixed, bounded speech plus the next trusted dialogue phase.

    ``phase`` and ``caller_requested_end`` must come from the trusted runtime.
    A standalone yes confirms an actual readback, but requests another turn after
    a local acknowledgement. Split corrections never count as standalone yes/no.
    """
    if phase not in _PHASES:
        raise ValueError("Unknown voicemail phase")
    if caller_requested_end:
        return RecoveryReply("Thank you. Goodbye.", True, "complete")
    if phase == "no_message":
        return RecoveryReply("I didn't hear a message. Please call again. Goodbye.", True, phase)
    if phase == "unconfirmed":
        return RecoveryReply("I heard your message but couldn't confirm its details. Goodbye.", True, phase)
    if phase in {"complete", "followup_timeout"}:
        return RecoveryReply(_FAREWELL, True, phase)
    if phase == "greeting":
        return RecoveryReply(VOICEMAIL_GREETING, False, phase)
    if phase == "capture":
        return RecoveryReply("Please continue with your message.", False, phase)

    original = _latest_caller_turn(context)
    if phase == "readback":
        if _has_speech(original):
            return RecoveryReply(f"Thanks for your message. {_FOLLOWUP_QUESTION}", False, "followup")
        return RecoveryReply("I didn't catch your message. Please tell me your message again.", False, phase)

    # Check before normalization: an injected marker or invisible control must
    # never become a standalone confirmation or request to finish.
    normalized = _normalized_reply(original)
    unclear = _contains_control(original) or normalized in _UNCLEAR or not _has_speech(original)
    if phase == "followup":
        if not unclear and normalized in _FOLLOWUP_DONE:
            return RecoveryReply(_FAREWELL, True, "complete")
        if not unclear and normalized in _FOLLOWUP_MORE:
            return RecoveryReply("Go ahead.", False, phase)
        if unclear:
            return RecoveryReply(_FOLLOWUP_QUESTION, False, phase)
        return RecoveryReply(f"Thanks for adding that. {_FOLLOWUP_QUESTION}", False, phase)

    if not unclear and normalized in _YES:
        return RecoveryReply(_FAREWELL, True, "complete")
    if not unclear and normalized in _NO:
        return RecoveryReply("Would you like to correct or add anything?", False, "followup")
    if unclear:
        return RecoveryReply("Was that correct?", False, phase)
    return RecoveryReply(f"Thanks for clarifying. {_FOLLOWUP_QUESTION}", False, "followup")
