"""Deterministic mission turns for tests. No model call and no phone call."""

from __future__ import annotations

from dataclasses import dataclass, field

from .brevity import assert_spoken_budget, exceeds_budget
from .research import load_utterances
from .schema import MissionPack


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
    supervisor: bool = False
    no_number_count: int = 0


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


def next_actions(pack: MissionPack, state: SimState, remote: str, *, secrets_configured: bool,
                 secrets: dict | None = None) -> list[Action]:
    """One remote utterance in. Covered forks are canned, not improvised."""
    if state.done:
        return []
    from .tree import ForkState, match_fork
    provided = dict(secrets or {})
    if secrets_configured and "pin" not in provided:
        from .secrets import Secret
        provided.setdefault("pin", Secret("CANCEL_ACCOUNT_PIN", "0000"))
        provided.setdefault("ssn_last4", Secret("CANCEL_SSN_LAST4", "0000"))
    fork = ForkState(
        notes=[{"kind": "confirmation", **note} if "kind" not in note else note
               for note in state.confirmed.values()],
        supervisor=state.supervisor,
        no_number_count=state.no_number_count,
    )
    plan = match_fork(pack, remote, fork, provided)
    actions: list[Action] = []
    if plan is None:
        _speak(actions, _fill(_bank(pack).get("open-cancel", "I'd like to cancel the line ending in {line_last4}."), pack), "unmatched")
        return actions
    if plan.supervisor:
        state.supervisor = True
    if plan.decision == "ivr-no-number":
        state.no_number_count += 1
    if plan.silent:
        actions.append(Action("silent", decision=plan.decision))
        return actions
    if plan.digits:
        actions.append(Action("dtmf", digits=plan.digits, decision=plan.decision))
        return actions
    for speech in plan.speech:
        kind = "confirmation" if plan.decision == "confirm" and "confirmation" in speech.lower() else None
        _speak(actions, speech, plan.decision, kind=kind)
    for note in plan.notes:
        actions.append(Action("note_confirmation", decision=plan.decision, note=dict(note)))
        if note.get("kind") == "confirmation":
            state.confirmed[note["line"]] = note
        if note.get("kind") == "retention":
            state.accepted = True
    if plan.hangup:
        actions.append(Action("hangup", decision=plan.decision))
        state.done = True
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
