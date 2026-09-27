"""Conservative call-level policy for live Modulate outcomes."""

from partner_detection import (
    DetectionObservation,
    DetectionReport,
    LiveDetectionOutcome,
    decide_call_detection,
)


def outcome(verdict, confidence, *, dropped=0, reason=None, session_id="CA-policy", stream_id="MZ-policy"):
    observation = DetectionObservation(
        session_id, stream_id, "inbound", 0, 4000,
        verdict, verdict, confidence,
    )
    report = DetectionReport(
        session_id=session_id,
        stream_id=stream_id,
        status=verdict,
        observations=(observation,),
        reason=reason,
        submitted_audio_ms=4000,
        elapsed_ms=5,
    )
    return LiveDetectionOutcome(report, accepted_frames=200, dropped_frames=dropped)


def test_confident_non_synthetic_evidence_is_retains_non_synthetic_label():
    decision = decide_call_detection([outcome("non-synthetic", 0.94)], min_confidence=0.80)

    assert decision.label == "non-synthetic"
    assert decision.confidence == 0.94
    assert decision.reason == "confident_non_synthetic"
    assert decision.session_id == "CA-policy"
    assert decision.streams == 1
    assert decision.observations == 1
    assert decision.accepted_frames == 200
    assert decision.dropped_frames == 0
    assert decision.submitted_audio_ms == 4000


def test_dropped_audio_forces_unknown_even_with_confident_provider_evidence():
    decision = decide_call_detection(
        [outcome("synthetic", 0.99, dropped=1)], min_confidence=0.80)

    assert decision.label == "unknown"
    assert decision.confidence is None
    assert decision.reason == "dropped_audio"
    assert decision.dropped_frames == 1


def test_conflicting_confident_streams_are_unknown():
    decision = decide_call_detection([
        outcome("non-synthetic", 0.95, stream_id="MZ-first"),
        outcome("synthetic", 0.91, stream_id="MZ-second"),
    ], min_confidence=0.80)

    assert decision.label == "unknown"
    assert decision.confidence is None
    assert decision.reason == "conflicting_evidence"
    assert decision.streams == 2


def test_incomplete_provider_result_cannot_become_a_decision():
    decision = decide_call_detection(
        [outcome("synthetic", 0.99, reason="provider_timeout")], min_confidence=0.80)

    assert decision.label == "unknown"
    assert decision.confidence is None
    assert decision.reason == "provider_incomplete"


def test_below_threshold_evidence_is_unknown():
    decision = decide_call_detection(
        [outcome("non-synthetic", 0.79)], min_confidence=0.80)

    assert decision.label == "unknown"
    assert decision.confidence is None
    assert decision.reason == "below_confidence_threshold"


def test_outcomes_from_different_calls_are_never_combined():
    decision = decide_call_detection([
        outcome("synthetic", 0.99, session_id="CA-first"),
        outcome("synthetic", 0.99, session_id="CA-second"),
    ], min_confidence=0.80)

    assert decision.session_id is None
    assert decision.label == "unknown"
    assert decision.reason == "mixed_sessions"


def test_missing_provider_outcomes_are_unknown():
    decision = decide_call_detection([], min_confidence=0.80)

    assert decision.session_id is None
    assert decision.label == "unknown"
    assert decision.confidence is None
    assert decision.reason == "no_results"
    assert decision.streams == 0


def test_no_usable_speech_content_is_unknown():
    report = DetectionReport(
        session_id="CA-policy", stream_id="MZ-policy",
        reason="no_usable_content", submitted_audio_ms=4000,
    )
    decision = decide_call_detection(
        [LiveDetectionOutcome(report, accepted_frames=200, dropped_frames=0)],
        min_confidence=0.80,
    )

    assert decision.label == "unknown"
    assert decision.confidence is None
    assert decision.reason == "no_usable_content"


def test_overlapping_synthetic_windows_use_the_lowest_qualified_confidence():
    decision = decide_call_detection([
        outcome("synthetic", 0.99, stream_id="MZ-first"),
        outcome("synthetic", 0.88, stream_id="MZ-first"),
    ], min_confidence=0.80)

    assert decision.label == "synthetic"
    assert decision.confidence == 0.88
    assert decision.reason == "confident_synthetic"
    assert decision.streams == 2


def test_unmapped_epochs_do_not_establish_a_call_verdict():
    decision = decide_call_detection([
        outcome("non-synthetic", 0.95, stream_id="MZ-first"),
        outcome("synthetic", 0.70, stream_id="MZ-second"),
    ], min_confidence=0.80)

    assert decision.label == "unknown"
    assert decision.confidence is None


def test_tiny_confident_observation_is_insufficient_evidence():
    from dataclasses import replace
    original = outcome("synthetic", .99)
    tiny = replace(original.report.observations[0], end_ms=20)
    sample = replace(original, report=replace(original.report, observations=(tiny,), submitted_audio_ms=20))
    decision = decide_call_detection([sample], min_confidence=.80)
    assert decision.label == "unknown"
    assert decision.reason == "insufficient_evidence"
    assert decision.confidence is None


def test_overlapping_windows_do_not_multiply_evidence_duration():
    from dataclasses import replace
    original = outcome("synthetic", .99)
    short = replace(original.report.observations[0], end_ms=2000)
    sample = replace(original, report=replace(original.report, observations=(short, short, short)))
    decision = decide_call_detection([sample], min_confidence=.80)
    assert decision.label == "unknown"
    assert decision.reason == "insufficient_evidence"


def test_no_content_report_cannot_smuggle_decisive_observations():
    decision = decide_call_detection([outcome("synthetic", .99, reason="no_usable_content")],
                                     min_confidence=.80)
    assert decision.label == "unknown"
    assert decision.reason == "no_usable_content"


def test_audio_cap_is_explicit_without_discarding_sufficient_evidence():
    from dataclasses import replace
    original = outcome("synthetic", .94)
    sample = replace(original, report=replace(original.report, coverage_limited=True))
    decision = decide_call_detection([sample], min_confidence=.80)
    assert decision.label == "synthetic"
    assert decision.coverage_limited is True
