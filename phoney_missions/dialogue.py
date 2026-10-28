"""Deterministic mission turns for tests. No model call and no phone call."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .brevity import assert_spoken_budget, exceeds_budget
from .ivr_policy import account_wipe, classify_dtmf, playbook_entries
from .policy import evaluate_retention
from .research import load_utterances
from .schema import MissionPack

_CONFIRM = re.compile(
    r"(?P<label>Apple Watch SE|BYOD mobile|watch|BYOD)?.{0,40}?"
    r"(?P<digits>\d{3}[-.\s]?\d{3}[-.\s]?\d{4}).{0,80}?"
    r"(?:confirmation(?: number)?|conf)\s*(?:is\s*)?(?P<code>[A-Za-z0-9-]{3,12}).{0,40}?"
    r"(?:effective(?: date)?\s*(?:is\s*)?(?P<date>20\d{2}-\d{2}-\d{2}))",
    re.IGNORECASE,
)
_PIN = re.compile(r"\b(pin|passcode|social|ssn|last four)\b", re.IGNORECASE)


@dataclass
class Action:
    tool: str
    speech: str = ""
    digits: str = ""
    note: dict | None = None
    decision: str = ""

    def __repr__(self) -> str:
        note = "" if not self.note else self.note.get("kind", "")
        return f"Action(tool={self.tool}, decision={self.decision}, digits_len={len(self.digits)}, note={note})"


@dataclass
class SimState:
    confirmed: dict = field(default_factory=dict)
    accepted: bool = False
    done: bool = False


def _bank(pack: MissionPack) -> dict[str, str]:
    return {str(row.get("id")): str(row["text"]) for row in load_utterances(pack)}


def _fill(text: str, pack: MissionPack, **extra) -> str:
    line = pack.lines[0]
    values = {
        "line_last4": line.last4,
        "line_display": line.display,
        "line_label": line.label,
        "account_name": pack.account_name,
    }
    values.update(extra)
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        return text


def _speak(state_actions: list[Action], text: str, decision: str, kind: str | None = None) -> None:
    speech = " ".join(text.split())
    if exceeds_budget(speech, kind):
        raise RuntimeError("simulator produced a spoken line over the brevity budget")
    state_actions.append(Action("speak", speech=speech, decision=decision))


def next_actions(pack: MissionPack, state: SimState, remote: str, *, secrets_configured: bool) -> list[Action]:
    """One remote utterance in, one or two short actions out."""
    if state.done:
        return []
    bank = _bank(pack)
    book = playbook_entries(pack)
    actions: list[Action] = []
    decision = classify_dtmf(remote, book)
    if decision.mode == "auto" and decision.digits:
        actions.append(Action("dtmf", digits=decision.digits, decision="ivr-auto"))
        return actions
    if decision.mode == "blocked" or account_wipe(remote):
        _speak(actions, bank.get("ivr-representative", "Representative."), "ivr-wipe")
        return actions
    if decision.mode == "model-only":
        line = bank.get("ivr-something-else") or bank.get("ivr-cancel") or "Cancel a line."
        _speak(actions, line, "ivr-model")
        return actions
    if _PIN.search(remote or "") and not secrets_configured:
        _speak(actions, bank.get("need-holder", "I need the account holder on the line."), "missing-secret")
        actions.append(Action("hangup", decision="missing-secret"))
        state.done = True
        return actions
    offer = evaluate_retention(remote, pack)
    if offer.is_offer and offer.accept:
        _speak(actions, bank.get("accept-retention", "I'll accept the monthly credit instead of canceling."), "accept-retention")
        actions.append(Action("note_confirmation", decision="accept-retention", note={
            "kind": "retention", "monthly_cents": offer.monthly_cents, "months": offer.months,
        }))
        actions.append(Action("hangup", decision="accept-retention"))
        state.accepted = True
        state.done = True
        return actions
    if offer.is_offer:
        _speak(actions, bank.get("decline-offer", "I'm not interested in offers."), "decline-retention")
        _speak(actions, _fill(bank.get("cancel-line", "Please cancel line {line_display}."), pack), "decline-retention")
        return actions
    found = _CONFIRM.search(remote or "")
    if found:
        digits = re.sub(r"\D", "", found.group("digits"))
        note = {
            "kind": "confirmation",
            "line": digits,
            "confirmation": found.group("code"),
            "effective": found.group("date"),
        }
        actions.append(Action("note_confirmation", decision="confirm", note=note))
        state.confirmed[digits] = note
        readback = _fill(
            bank.get("readback", "Confirmed. Line {line_display}, confirmation {code}, effective {date}."),
            pack, line_display=_dash(digits), code=note["confirmation"], date=note["effective"],
        )
        _speak(actions, readback, "confirm", kind="confirmation")
        if all(line.digits in state.confirmed for line in pack.lines):
            _speak(actions, bank.get("goodbye", "Thank you, goodbye."), "done")
            actions.append(Action("hangup", decision="done"))
            state.done = True
        return actions
    if re.search(r"\bsomething else\b", remote or "", re.I):
        _speak(actions, bank.get("ivr-something-else", "Something else."), "ivr-speech")
        return actions
    _speak(actions, _fill(bank.get("open-cancel", "I'd like to cancel the line ending in {line_last4}."), pack), "continue-cancel")
    return actions


def _dash(digits: str) -> str:
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return digits


def simulate(pack: MissionPack, utterances: list[str], *, secrets_configured: bool = False) -> list[list[Action]]:
    state = SimState()
    turns = []
    for remote in utterances:
        actions = next_actions(pack, state, remote, secrets_configured=secrets_configured)
        for action in actions:
            if action.tool == "speak":
                assert_spoken_budget(action.speech)
        turns.append(actions)
    return turns
