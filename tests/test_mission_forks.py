"""Canned T-Mobile forks. No dialer and no Twilio."""

import json
from pathlib import Path

from phoney_missions.brevity import assert_spoken_budget, sentences
from phoney_missions.dialogue import simulate
from phoney_missions.schema import load_pack
from phoney_missions.secrets import Secret
from phoney_missions.tree import ForkState, match_fork


ROOT = Path("docs/missions/tmobile-cancel")
REQUIRED = {
    "open-cancel", "open-watch", "next-line", "which-line", "only-named", "leave-others",
    "decline-offer", "cancel-line", "decline-plan", "promo-still-cancel", "eip-ok",
    "ask-confirm", "readback", "accept-retention", "need-holder", "say-account",
    "say-name", "say-pin", "say-ssn", "say-zip", "goodbye", "partial-close",
    "ivr-english", "ivr-no-number", "ivr-something-else", "ivr-cancel",
    "ivr-representative", "wrong-department", "supervisor", "unpair-watch", "watch-separate",
}


def _pack():
    return load_pack("tmobile-cancel")


def _plan(text, **kwargs):
    return match_fork(_pack(), text, kwargs.get("state") or ForkState(), kwargs.get("secrets"))


def test_utterance_bank_is_one_sentence_and_covers_the_tree():
    document = json.loads((ROOT / "utterances.json").read_text())
    tree = (ROOT / "CONVERSATION_TREE.md").read_text()
    ids = set()
    sample = {
        "line_last4": "2659", "line_display": "773-988-2659", "line_label": "Apple Watch SE",
        "account_name": "James", "account_number": "252027408", "billing_zip": "97201",
        "pin": "0000", "ssn_last4": "0000", "code": "AB12", "date": "2026-11-15",
    }
    for row in document["lines"]:
        ids.add(row["id"])
        assert f"`{row['id']}`" in tree
        text = row["text"].format(**sample)
        kind = "confirmation" if row["kind"] == "confirmation" else "default"
        assert_spoken_budget(text, kind)
        if kind != "confirmation":
            assert len(sentences(text)) == 1
    assert REQUIRED <= ids
    raw = (ROOT / "utterances.json").read_text() + (ROOT / "CONVERSATION_TREE.md").read_text()
    assert "0000" not in raw
    assert "CANCEL_ACCOUNT_PIN" not in (ROOT / "utterances.json").read_text()


def test_high_risk_forks_are_canned():
    pack = _pack()
    decline = _plan("We can offer $10 a month for 6 months.")
    assert decline.decision == "decline-retention" and not decline.hangup
    assert any("not interested" in line.lower() for line in decline.speech)
    accept = _plan("I can do $20 per month for 12 months if you keep the lines.")
    assert accept.hangup and accept.notes[0]["monthly_cents"] == 2000
    one_time = _plan("Here is a one-time $50 bill credit.")
    assert one_time.decision == "decline-retention"
    cheaper = _plan("We can move you to a cheaper plan.")
    assert cheaper.decision == "decline-plan"
    assert "cancel" in cheaper.speech[0].lower()
    scare = _plan("You'll never get this offer again.")
    assert scare.decision == "ignore-scare"
    promo = _plan("Your multi-line discount will change if you cancel.")
    assert promo.decision == "promo-still-cancel"
    eip = _plan("Device payments will continue on that line.")
    assert eip.decision == "eip-ok"
    unpair = _plan("Please unpair the watch before we cancel it.")
    assert unpair.decision == "unpair-watch"
    watch = _plan("Porting the phone already canceled the watch.")
    assert watch.decision == "watch-separate"
    hold = _plan("Please hold, your call is important to us.")
    assert hold.silent and not hold.speech
    missing = _plan("What is your account PIN?")
    assert missing.hangup and missing.decision == "missing-secret"
    assert "0000" not in " ".join(missing.speech)
    otp = _plan("I just sent a verification code by text.")
    assert otp.hangup and otp.decision == "missing-secret"
    whole = _plan("Do you want to port out the whole account?")
    assert whole.decision == "only-named" and len(whole.speech) == 2
    supervisor = _plan("I can't cancel that line.")
    assert supervisor.supervisor and not supervisor.hangup
    again = _plan("I still can't cancel that line.", state=ForkState(supervisor=True))
    assert again.hangup and again.decision == "partial-close"
    account = _plan("Please enter your account number.")
    assert account.digits == "252027408"
    mobile = _plan("Enter or say your mobile number.")
    assert mobile.digits == "7739882659"
    first = _plan(
        "The Apple Watch SE line 773-988-2659 is cancelled. "
        "Your confirmation number is AB12 and the effective date is 2026-11-15.")
    assert first.notes[0]["line"] == "7739882659"
    assert any(line.startswith("Next,") for line in first.speech)
    assert not first.hangup
    second_state = ForkState(notes=[{"kind": "confirmation", "line": "7739882659"}])
    second = _plan(
        "The BYOD line 312-545-2413 is cancelled. Confirmation number CD34 effective date 2026-11-15.",
        state=second_state)
    assert second.hangup and second.notes[0]["line"] == "3125452413"
    pin = match_fork(pack, "What is the PIN?", ForkState(),
                     {"pin": Secret("CANCEL_ACCOUNT_PIN", "0000")})
    assert pin.decision == "say-pin" and not pin.hangup
    assert Secret("CANCEL_ACCOUNT_PIN", "0000").leaked_into(pin.speech[0])


def test_simulator_decline_then_second_line_stays_short():
    pack = _pack()
    turns = simulate(pack, [
        "Press 0 to speak to a representative.",
        "We can offer a one-time $100 credit.",
        "The Apple Watch SE line 773-988-2659 is cancelled. Your confirmation number is AB12 and the effective date is 2026-11-15.",
        "How can I help with the next line?",
    ])
    assert turns[0][0].tool == "dtmf"
    assert any(action.decision == "decline-retention" for action in turns[1])
    assert not any(action.tool == "hangup" for action in turns[1])
    assert any(action.note and action.note.get("line") == "7739882659" for action in turns[2])
    assert turns[3][0].decision == "open-cancel"
    for turn in turns:
        for action in turn:
            if action.speech:
                assert_spoken_budget(action.speech)
