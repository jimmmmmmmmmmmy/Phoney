"""Deterministic THEY-say to WE-say map for a mission.

Covered forks never go to the model. Unmatched menus return None so the model
can recover in one short sentence. PIN and SSN are filled only at speak time.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .ivr_policy import account_wipe, classify_dtmf, playbook_entries
from .policy import evaluate_retention
from .research import load_utterances
from .schema import MissionPack
from .secrets import Secret, load_secret

_CONFIRM = re.compile(
    r".{0,80}?(?P<digits>\d{3}[-.\s]?\d{3}[-.\s]?\d{4}).{0,80}?"
    r"(?:confirmation(?: number)?|conf)\s*(?:is\s*)?(?P<code>[A-Za-z0-9-]{3,12}).{0,40}?"
    r"(?:effective(?: date)?\s*(?:is\s*)?(?P<date>20\d{2}-\d{2}-\d{2}))",
    re.IGNORECASE,
)
_SECRET_KEYS = ("pin", "ssn_last4", "ssn", "password", "otp")


@dataclass
class ForkState:
    notes: list[dict] = field(default_factory=list)
    supervisor: bool = False


@dataclass
class ForkPlan:
    decision: str
    speech: tuple[str, ...] = ()
    digits: str = ""
    notes: tuple[dict, ...] = ()
    hangup: bool = False
    silent: bool = False
    supervisor: bool = False

    def model_text(self) -> str:
        """Text the speech path already understands. Not for logs."""
        parts = list(self.speech)
        for note in self.notes:
            if note.get("kind") == "confirmation":
                parts.append(
                    f"[/NOTE confirm:{note['line']}:{note['confirmation']}:{note['effective']}]")
            elif note.get("kind") == "retention":
                months = note.get("months") if note.get("months") is not None else "ongoing"
                parts.append(f"[/NOTE retention:{note['monthly_cents']}:{months}]")
        if self.hangup:
            parts.append("[/END CALL]")
        return "\n".join(parts)


def _bank(pack: MissionPack) -> dict[str, str]:
    return {str(row.get("id")): str(row["text"]) for row in load_utterances(pack)}


def _fold(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _has(text: str, *phrases: str) -> bool:
    folded = _fold(text)
    return any(phrase in folded for phrase in phrases)


def _confirmed(state: ForkState) -> set[str]:
    return {str(note.get("line")) for note in state.notes if note.get("kind") == "confirmation"}


def _open_lines(pack: MissionPack, state: ForkState):
    done = _confirmed(state)
    return [line for line in pack.lines if line.digits not in done]


def _values(pack: MissionPack, state: ForkState, secrets: dict[str, Secret], **extra) -> dict:
    line = _open_lines(pack, state)
    current = line[0] if line else pack.lines[0]
    pin = secrets.get("pin")
    last4 = secrets.get("ssn_last4")
    values = {
        "line_last4": current.last4,
        "line_display": current.display,
        "line_label": current.label,
        "account_name": pack.account_name,
        "account_number": pack.account_number,
        "billing_zip": pack.billing_zip,
        "pin": pin.reveal() if pin and pin.configured else "",
        "ssn_last4": last4.reveal() if last4 and last4.configured else "",
        "code": "",
        "date": "",
    }
    values.update(extra)
    return values


def _say(bank: dict[str, str], ident: str, values: dict) -> str:
    return " ".join(bank[ident].format(**values).split())


def _secret_ready(secrets: dict[str, Secret], slot: str) -> bool:
    secret = secrets.get(slot)
    return bool(secret and secret.configured)


def match_fork(pack: MissionPack, remote: str, state: ForkState,
               secrets: dict[str, Secret] | None = None) -> ForkPlan | None:
    """Return a covered fork, or None when the model may answer."""
    secrets = secrets or {}
    bank = _bank(pack)
    if not bank or not str(remote or "").strip():
        return None
    values = _values(pack, state, secrets)
    book = playbook_entries(pack)
    text = str(remote)

    if account_wipe(text) and re.search(r"\bpress\b", text, re.IGNORECASE):
        return ForkPlan("ivr-wipe", speech=(_say(bank, "ivr-representative", values),))

    dtmf = classify_dtmf(text, book)
    if dtmf.mode == "auto" and dtmf.digits:
        return ForkPlan("ivr-auto", digits=dtmf.digits)

    if _has(text, "please hold", "your call is important", "hold music", "one moment"):
        return ForkPlan("ivr-hold", silent=True)

    if _has(text, "para espanol", "for spanish", "for english", "which language"):
        return ForkPlan("ivr-english", speech=(_say(bank, "ivr-english", values),))

    if _has(text, "mobile number", "phone number you're calling", "10-digit", "ten digit"):
        opened = _open_lines(pack, state)
        if opened:
            return ForkPlan("ivr-line-digits", digits=opened[0].digits)
        return ForkPlan("ivr-no-number", speech=(_say(bank, "ivr-no-number", values),))

    if _has(text, "account number"):
        if _has(text, "enter", "dial", "press", "keypad"):
            return ForkPlan("account-dtmf", digits=pack.account_number)
        return ForkPlan("say-account", speech=(_say(bank, "say-account", values),))

    if re.search(r"\b(one[- ]time code|verification code|text code|otp)\b", text, re.IGNORECASE):
        return ForkPlan("missing-secret", speech=(_say(bank, "need-holder", values),), hangup=True)

    if re.search(r"\b(passcode|pin)\b", text, re.IGNORECASE):
        if _secret_ready(secrets, "pin"):
            return ForkPlan("say-pin", speech=(_say(bank, "say-pin", values),))
        return ForkPlan("missing-secret", speech=(_say(bank, "need-holder", values),), hangup=True)

    if _has(text, "social security", "ssn", "last four", "last 4"):
        if _secret_ready(secrets, "ssn_last4"):
            return ForkPlan("say-ssn", speech=(_say(bank, "say-ssn", values),))
        return ForkPlan("missing-secret", speech=(_say(bank, "need-holder", values),), hangup=True)

    if _has(text, "your name", "account name", "name on the account"):
        return ForkPlan("say-name", speech=(_say(bank, "say-name", values),))

    if _has(text, "zip code", "zipcode", "billing zip", "postal code"):
        if pack.billing_zip:
            return ForkPlan("say-zip", speech=(_say(bank, "say-zip", values),))
        return ForkPlan("missing-secret", speech=(_say(bank, "need-holder", values),), hangup=True)

    if _has(text, "unpair"):
        return ForkPlan("unpair-watch", speech=(_say(bank, "unpair-watch", values),))

    if _has(text, "watch") and _has(text, "already cancel", "already canceled", "port"):
        return ForkPlan("watch-separate", speech=(_say(bank, "watch-separate", values),))

    if _has(text, "cheaper plan", "cheaper line", "lower plan", "different plan", "change your plan"):
        return ForkPlan("decline-plan", speech=(_say(bank, "decline-plan", values),))

    if _has(text, "never get this", "won't get this offer", "will not get this offer", "last chance"):
        return ForkPlan("ignore-scare", speech=(_say(bank, "cancel-line", values),))

    if _has(text, "multi-line discount", "promotion will", "promo will", "discount will", "lose your discount"):
        return ForkPlan("promo-still-cancel", speech=(_say(bank, "promo-still-cancel", values),))

    if _has(text, "device payment", "installment", "eip", "payments will continue"):
        return ForkPlan("eip-ok", speech=(_say(bank, "eip-ok", values),))

    offer = evaluate_retention(text, pack)
    if offer.is_offer and offer.accept:
        return ForkPlan(
            "accept-retention",
            speech=(_say(bank, "accept-retention", values), _say(bank, "goodbye", values)),
            notes=({"kind": "retention", "monthly_cents": offer.monthly_cents, "months": offer.months},),
            hangup=True,
        )
    if offer.is_offer:
        return ForkPlan(
            "decline-retention",
            speech=(
                _say(bank, "decline-offer", values),
                _say(bank, "cancel-line", values),
                _say(bank, "ask-confirm", values),
            ),
        )

    found = _CONFIRM.search(text)
    if found:
        digits = re.sub(r"\D", "", found.group("digits"))
        code, date = found.group("code"), found.group("date")
        note = {"kind": "confirmation", "line": digits, "confirmation": code, "effective": date}
        filled = _values(pack, state, secrets, line_display=_dash(digits), code=code, date=date)
        remaining = [line for line in pack.lines if line.digits not in _confirmed(state) | {digits}]
        speech = [_say(bank, "readback", filled)]
        hangup = False
        if remaining:
            nxt = _values(pack, ForkState(notes=[*state.notes, note]), secrets)
            speech.append(_say(bank, "next-line", nxt))
        else:
            speech.append(_say(bank, "goodbye", values))
            hangup = True
        return ForkPlan("confirm", speech=tuple(speech), notes=(note,), hangup=hangup)

    if _has(text, "cannot cancel", "can't cancel", "unable to cancel", "not able to cancel",
            "won't cancel", "will not cancel", "refuse to cancel"):
        if state.supervisor:
            return ForkPlan("partial-close", speech=(_say(bank, "partial-close", values),
                                                    _say(bank, "goodbye", values)), hangup=True)
        return ForkPlan("supervisor", speech=(_say(bank, "supervisor", values),), supervisor=True)

    if _has(text, "which line", "what line", "which number", "what number"):
        return ForkPlan("which-line", speech=(_say(bank, "which-line", values),))

    if _has(text, "whole account", "entire account", "all the lines", "port out", "port your",
            "suspend", "close the account"):
        return ForkPlan("only-named", speech=(_say(bank, "only-named", values),
                                             _say(bank, "leave-others", values)))

    if _has(text, "wrong department", "transferred you", "billing department"):
        return ForkPlan("wrong-department", speech=(_say(bank, "wrong-department", values),))

    if _has(text, "how can i help", "how may i help", "what can i help", "how can we help"):
        opened = _open_lines(pack, state)
        current = opened[0] if opened else pack.lines[0]
        if "watch" in current.label.lower():
            return ForkPlan("open-watch", speech=(_say(bank, "open-watch", values),))
        return ForkPlan("open-cancel", speech=(_say(bank, "open-cancel", values),))

    if dtmf.mode == "model-only":
        if _has(text, "cancel"):
            return ForkPlan("ivr-cancel", speech=(_say(bank, "ivr-cancel", values),))
        return ForkPlan("ivr-model", speech=(_say(bank, "ivr-something-else", values),))

    if _has(text, "something else"):
        return ForkPlan("ivr-something-else", speech=(_say(bank, "ivr-something-else", values),))

    if _has(text, "welcome to t-mobile", "welcome to tmobile"):
        return ForkPlan("ivr-cancel", speech=(_say(bank, "ivr-cancel", values),))

    return None


def _dash(digits: str) -> str:
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return digits


def plan_session(session):
    """Covered fork for a live mission session, or None to let the model speak."""
    from .bridge import MISSION_AGENT_KIND, load_for_goal
    from voice_stack.cancel_agent import CANCEL_AGENT_KIND
    if getattr(session, "agent_kind", "") not in {MISSION_AGENT_KIND, CANCEL_AGENT_KIND}:
        return None
    try:
        pack = load_for_goal(session.goal)
    except Exception:
        return None
    secrets = {}
    for slot, env_name in pack.secret_env.items():
        if slot in _SECRET_KEYS or "PASSWORD" in env_name:
            if "PASSWORD" in env_name:
                continue
            secrets[slot] = load_secret(os.environ, env_name)
    state = ForkState(
        notes=list(getattr(session, "mission_notes", []) or []),
        supervisor=bool(getattr(session, "mission_supervisor", False)),
    )
    latest = next((turn.get("text", "") for turn in reversed(session.turns)
                   if turn.get("speaker") == "remote"), "")
    return match_fork(pack, latest, state, secrets)
