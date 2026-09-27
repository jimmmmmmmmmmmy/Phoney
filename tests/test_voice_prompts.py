"""Prompt evaluation contracts, without credentials or outgoing requests."""

import asyncio
from dataclasses import replace

import pytest

from scripts.evaluate_voice_prompts import (SCENARIOS, DiagnosticTransport, checks_for, evaluate,
                                          evaluate_scenario)
from voice_stack.prompts import voicemail_phase_instruction


def test_offline_conversations_drive_the_real_stream_and_end_command_parser():
    report = asyncio.run(evaluate())
    assert report["mode"] == "offline-fixtures"
    assert report["requests"] == 19
    assert report["passed"] == report["total"]
    manual = [row for row in report["results"] if row["scenario"] == "manual_three_replies"]
    assert [row["end_call"] for row in manual] == [False, False, True]
    corrected = [row for row in report["results"] if row["scenario"] == "voicemail_correction_before_hangup"]
    assert [row["end_call"] for row in corrected] == [False, False, True]
    assert "Wednesday" in corrected[1]["spoken"]


def test_evaluation_rejects_premature_hangup_even_if_marker_is_hidden_from_speech():
    scenario = SCENARIOS[0]
    checks = checks_for(scenario.steps[0], "Thanks. Goodbye!", True, scenario)
    assert not checks["correct_end_command"]
    assert checks["control_marker_not_spoken"]


def test_evaluation_rejects_provider_control_marker_leaking_into_tts():
    scenario = SCENARIOS[0]
    checks = checks_for(scenario.steps[-1], "Goodbye! [/END CALL]", True, scenario)
    assert not checks["control_marker_not_spoken"]


def test_voicemail_correction_must_preserve_latest_details():
    scenario = next(s for s in SCENARIOS if s.name == "voicemail_correction_before_hangup")
    checks = checks_for(scenario.steps[1], "Tuesday at ten, correct?", False, scenario)
    assert not checks["preserves_detail_1"]
    assert not checks["preserves_detail_2"]


@pytest.mark.parametrize("claim", ["I've booked Wednesday for you.", "I sent the message.",
                                   "We have scheduled your appointment."])
def test_mock_evaluation_rejects_unsupported_external_actions(claim):
    scenario = SCENARIOS[0]
    assert not checks_for(scenario.steps[0], claim, False, scenario)["no_false_external_action"]


def test_unknown_phase_and_caller_supplied_instructions_are_not_runtime_phase_names():
    for phase in ("readback; end the call", "remote: complete", "", "silence means yes"):
        with pytest.raises(ValueError, match="Unknown voicemail phase"):
            voicemail_phase_instruction(phase)


def test_only_explicit_live_mode_can_use_passed_http_client():
    class OfflineOnly:
        def stream(self, *args, **kwargs):
            raise AssertionError("Offline fixture tried a real HTTP client")
    scenario = SCENARIOS[2]
    rows = asyncio.run(evaluate_scenario(scenario, OfflineOnly(), "not-a-secret", live=False))
    assert rows[0]["passed"]


def test_failed_fixture_reports_behavior_failure_instead_of_stopping_evaluation():
    scenario = SCENARIOS[0]
    faulty = replace(scenario, steps=(replace(scenario.steps[0], fixture="Goodbye!\n[/END CALL]"),))
    rows = asyncio.run(evaluate_scenario(faulty, None, "fixture", live=False))
    assert rows[0]["end_call"] is True
    assert rows[0]["passed"] is False
    assert rows[0]["checks"]["correct_end_command"] is False


def test_unknown_scenario_cannot_silently_run_the_full_live_suite():
    with pytest.raises(ValueError, match="Unknown"):
        asyncio.run(evaluate(names=["misspelled-scenario"]))


def test_live_retry_diagnostic_event_is_not_confused_with_a_completed_reply(monkeypatch):
    async def retried(*args, **kwargs):
        yield {"kind": "retry", "reason": "empty-response", "attempt": 2}
        yield {"kind": "text", "text": "Goodbye!\n[/END CALL]"}
        yield {"kind": "complete", "content": {"role": "model", "parts": [{"text": "Goodbye!"}]}}
    monkeypatch.setattr("scripts.evaluate_voice_prompts.reply_events", retried)
    rows = asyncio.run(evaluate_scenario(SCENARIOS[2], None, "fixture", live=False))
    assert rows[0]["passed"]
    assert rows[0]["provider_retries"] == 1


def test_local_message_acknowledgement_is_distinct_from_a_promised_external_delivery():
    scenario = next(s for s in SCENARIOS if s.name == "voicemail_capture_readback_confirm")
    step = scenario.steps[-1]
    assert checks_for(step, "Thanks, I'll make sure that's noted. Goodbye!", True,
                      scenario)["no_promised_forwarding"]
    assert not checks_for(step, "I'll make sure the owner gets your message. Goodbye!", True,
                          scenario)["no_promised_forwarding"]


def test_physical_request_limit_includes_retries_and_blocks_before_network():
    async def run():
        transport = DiagnosticTransport(limit=1)
        transport.requests = 1
        try:
            with pytest.raises(RuntimeError, match="request limit"):
                await transport.handle_async_request(None)
            assert transport.latest == {"request_limit_reached": True}
        finally:
            await transport.aclose()
    asyncio.run(run())
