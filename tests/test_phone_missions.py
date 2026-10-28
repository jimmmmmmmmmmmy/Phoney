"""Mission packs, brevity, research, gates, and a CLI that does not dial."""

import json
from pathlib import Path

import httpx

from phoney_missions.brevity import BANS, assert_spoken_budget, exceeds_budget, fit_spoken_phrase, instruction
from phoney_missions.brief import assemble_brief
from phoney_missions.cli import main
from phoney_missions.dialogue import simulate
from phoney_missions.gate import live_call_block, unattended_dial_permitted
from phoney_missions.ivr_policy import classify_dtmf, playbook_entries
from phoney_missions.research import ResearchError, assert_research_clean, build_cache, write_cache
from phoney_missions.schema import MissionSchemaError, load_pack, load_template, parse_pack
from phoney_missions.secrets import Secret, assert_no_leak
from voice_stack.agent import ReplyCommandBuffer
from voice_stack.latency import LatencyTrace, budgets, select_stt, select_voice
from voice_stack.settings import VoiceSettings


def test_pack_schema_template_and_secret_rejection():
    pack = load_pack("tmobile-cancel")
    assert pack.brevity == "strict"
    assert pack.destination_e164 == "+18009378997"
    assert pack.retention.min_monthly_reduction_cents == 2000
    template = load_template()
    parsed = parse_pack(template)
    assert parsed.brevity == "strict" and "dtmf" in parsed.tools
    broken = json.loads(json.dumps(template))
    broken["password"] = "hunter2"
    try:
        parse_pack(broken)
    except MissionSchemaError as exc:
        assert "hunter2" not in str(exc)
    else:
        raise AssertionError("password field was accepted")
    literal = json.loads(json.dumps(template))
    literal["secrets"]["ssn_last4"] = "9876"
    try:
        parse_pack(literal)
    except MissionSchemaError as exc:
        assert "9876" not in str(exc)
    else:
        raise AssertionError("literal SSN was accepted")
    missing = json.loads(json.dumps(template))
    missing.pop("brevity")
    try:
        parse_pack(missing)
    except MissionSchemaError as exc:
        assert "brevity" in str(exc)
    else:
        raise AssertionError("missing brevity was accepted")


def test_brief_hides_secrets_from_repr_and_public_text():
    pack = load_pack("tmobile-cancel")
    secret = Secret("CANCEL_SSN_LAST4", "9876")
    pin = Secret("CANCEL_ACCOUNT_PIN", "43210")
    brief = assemble_brief(pack, {
        "CANCEL_SSN_LAST4": "9876",
        "CANCEL_ACCOUNT_PIN": "43210",
    })
    assert brief.secret_configured("ssn_last4") and brief.secret_configured("pin")
    assert not brief.public_contains_configured_secrets()
    brief.assert_public_safe()
    assert "9876" not in repr(brief) and "43210" not in repr(brief)
    assert "9876" not in repr(secret) and "43210" not in repr(pin)
    assert "20.00" in brief.public_text() and "12 months" in brief.public_text()
    for ban in BANS:
        assert ban in brief.public_text()
    assert "I'd like to cancel the line ending in 2659." in brief.public_text()
    try:
        assert_no_leak("9876", [secret])
    except AssertionError as exc:
        assert "9876" not in str(exc)
        assert "CANCEL_SSN_LAST4" in str(exc)
    else:
        raise AssertionError("leak check did not fire")
    changed = parse_pack({**json.loads(Path("phoney_missions/packs/tmobile-cancel.json").read_text()),
                          "policy": {**json.loads(Path("phoney_missions/packs/tmobile-cancel.json").read_text())["policy"],
                                     "retention": {"accept_if": "lasting_bill_cut",
                                                   "min_monthly_reduction_cents": 2500,
                                                   "min_months": 6,
                                                   "decline_one_time_credits": True}}})
    assert "25.00" in assemble_brief(changed, {}, include_research=False).public_text()
    assert "6 months" in assemble_brief(changed, {}, include_research=False).public_text()


def test_ivr_auto_model_only_and_wipe():
    book = playbook_entries(load_pack("tmobile-cancel"))
    auto = classify_dtmf("Press 0 to speak to a representative.", book)
    assert auto.mode == "auto" and auto.digits == "0"
    held = classify_dtmf("Press 1 to cancel a line.", book)
    assert held.mode == "model-only" and held.digits == ""
    blocked = classify_dtmf("Press 9 to cancel all lines.", book)
    assert blocked.mode == "blocked" and blocked.digits == ""
    hidden = ReplyCommandBuffer()
    spoken = hidden.feed("Confirmed.\n[/NOTE confirm:7739882659:AB12:2026-11-15]\n")
    hidden.finish()
    assert "NOTE" not in spoken and hidden.notes == ["confirm:7739882659:AB12:2026-11-15"]


def test_gates_and_simulated_cancel_retention_paths():
    assert unattended_dial_permitted({"PHONEY_UNATTENDED": "1"}, mission=True)[0] is False
    assert unattended_dial_permitted({"PHONEY_ALLOW_LIVE_CALL": "1"}, mission=True)[0] is False
    assert unattended_dial_permitted({
        "PHONEY_UNATTENDED": "1", "PHONEY_ALLOW_LIVE_CALL": "1", "CI": "1",
    }, mission=True)[0] is False
    assert unattended_dial_permitted({
        "PHONEY_UNATTENDED": "1", "PHONEY_ALLOW_LIVE_CALL": "1",
    }, mission=False)[0] is False
    ok, _ = unattended_dial_permitted({
        "PHONEY_UNATTENDED": "1", "PHONEY_ALLOW_LIVE_CALL": "1",
    }, mission=True)
    assert ok
    assert live_call_block("http://127.0.0.1:8000", {"CI": "1", "PHONEY_ALLOW_LIVE_CALL": "1"})
    pack = load_pack("tmobile-cancel")
    cancel = simulate(pack, [
        "Press 0 to speak to a representative.",
        "We can offer a one-time $50 credit.",
        "The Apple Watch SE line 773-988-2659 is cancelled. Your confirmation number is AB12 and the effective date is 2026-11-15.",
        "The BYOD mobile line 312-545-2413 is cancelled. Confirmation number CD34 effective date 2026-11-15.",
    ])
    assert cancel[0][0].tool == "dtmf" and cancel[0][0].digits == "0"
    assert any(action.decision == "decline-retention" for action in cancel[1])
    assert not any(action.tool == "hangup" for action in cancel[1])
    assert cancel[-1][-1].tool == "hangup"
    assert {action.note["line"] for turn in cancel for action in turn if action.note and action.note.get("line")} == {
        "7739882659", "3125452413"}
    kept = simulate(pack, ["I can do $20 per month for 12 months if you keep the lines."])
    assert any(action.tool == "hangup" and action.decision == "accept-retention" for action in kept[0])
    short = simulate(pack, ["We can do $10 a month for 6 months."])
    assert any(action.decision == "decline-retention" for action in short[0])
    for turn in cancel + kept + short:
        for action in turn:
            if action.tool == "speak":
                assert_spoken_budget(action.speech)
    ramble = "I completely understand how frustrating this is and I want to review every option before we decide anything at all today."
    assert exceeds_budget(ramble)
    assert not exceeds_budget(fit_spoken_phrase(ramble))
    assert "No filler." in instruction()


def test_research_cache_and_cli_do_not_dial(tmp_path, monkeypatch, capsys):
    pack = load_pack("tmobile-cancel")
    text = Path(pack.research_doc).read_text()
    assert "https://www.t-mobile.com/support/account/cancel-service" in text
    assert "https://www.t-mobile.com/contact-us" in text
    assert "2026-10-29" in text
    assert "Not legal advice" in text or "not legal advice" in text
    try:
        assert_research_clean("secret 9876", {"CANCEL_SSN_LAST4": "9876"})
    except ResearchError as exc:
        assert "9876" not in str(exc)
    else:
        raise AssertionError("research accepted a secret")
    path = write_cache(pack, tmp_path / "cache.json", {"CANCEL_SSN_LAST4": "9876", "CANCEL_ACCOUNT_PIN": "43210"})
    assert_no_leak(path.read_text(), [
        Secret("CANCEL_SSN_LAST4", "9876"), Secret("CANCEL_ACCOUNT_PIN", "43210")])
    assert build_cache(pack)["x_refresh_after"] == "2026-10-29T00:00:00Z"
    called = []
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: called.append(1))
    monkeypatch.delenv("PHONEY_ALLOW_LIVE_CALL", raising=False)
    monkeypatch.delenv("PHONEY_UNATTENDED", raising=False)
    assert main(["check", "tmobile-cancel"]) == 0
    assert main(["place-call", "tmobile-cancel"]) == 2
    assert main(["run", "tmobile-cancel"]) == 0
    assert called == []
    out = capsys.readouterr().out
    assert "no call placed" in out
    assert "9876" not in out and "43210" not in out


def test_deepgram_is_the_only_stt_and_budgets_match():
    class Bare:
        deepgram_api_key = ""
        deepgram_model = "nova-3"
        transcription_enabled = False
    assert select_stt(Bare()).get("provider") == "unconfigured"
    assert select_stt(Bare()).get("alternatives") == []
    class Keyed:
        deepgram_api_key = "dg-test"
        deepgram_model = "nova-3"
        transcription_enabled = True
    chosen = select_stt(Keyed())
    assert chosen["provider"] == "deepgram" and chosen["preferred"] == "deepgram"
    voice = VoiceSettings(
        enabled=False, tts_provider="phonon", phonon_base_url="http://127.0.0.1:8091",
        llm_provider="nemotron", nemotron_base_url="http://127.0.0.1:8088",
        gemini_api_key="g", elevenlabs_api_key="e", elevenlabs_voice_id="voice1")
    plan = select_voice(voice, Keyed())
    assert plan["stt"] == "deepgram" and plan["tts"] == "phonon" and plan["llm"] == "nemotron"
    assert plan["tts_fallback"] == "elevenlabs" and plan["llm_fallback"] == "gemini"
    table = budgets()
    assert table["stt_provider"] == "deepgram"
    assert table["stt_endpointing_ms"] == 750
    assert table["llm_first_text_seconds"] == 3.0
    clock = iter([10.0, 10.2])
    trace = LatencyTrace(clock=lambda: next(clock))
    trace.mark("stt-final")
    trace.mark("first-audio")
    assert trace.ms("stt-final", "first-audio") == 200
