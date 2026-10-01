"""Focused product and boundary checks; test helpers live in support."""

from partner_detection import decide_call_detection

from support.detection_policy import outcome


def test_short_ai_segment_in_long_natural_call_is_not_diluted_by_human_speech():
    from dataclasses import replace
    original = outcome("synthetic", .95)
    natural = replace(original.report.observations[0], start_ms=4000, end_ms=120000,
                      verdict="non-synthetic", provider_verdict="non-synthetic", confidence=.99)
    sample = replace(original, report=replace(original.report,
                     observations=(*original.report.observations, natural), submitted_audio_ms=120000))
    decision = decide_call_detection([sample], min_confidence=.80)
    assert decision.label == "synthetic"
    assert decision.reason == "confident_synthetic"
    assert decision.confidence == .95
