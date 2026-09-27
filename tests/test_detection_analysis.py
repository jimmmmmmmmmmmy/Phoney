"""Duration weighting, uncertainty, and threshold boundaries are explicit policy."""

import pytest

from partner_detection.analysis import build_analysis, validate_analysis, MAX_WINDOWS


def window(start, end, verdict="synthetic", confidence=.94, stream="MZ-one"):
    return dict(stream_id=stream, start_ms=start, end_ms=end, verdict=verdict, confidence=confidence)


@pytest.mark.parametrize("synthetic, expected", [(1999, "none"), (2000, "potential_ai"),
                                                 (2999, "potential_ai"), (3000, "ai_caller"),
                                                 (4000, "ai_caller")])
def test_exact_duration_threshold_boundaries(synthetic, expected):
    windows = [window(0, synthetic)]
    if synthetic < 4000:
        windows.append(window(synthetic, 4000, "non-synthetic"))
    result = build_analysis(windows)
    assert result["alert"] == expected
    assert result["synthetic_ms"] == synthetic
    assert result["analyzed_ms"] == 4000
    assert result["synthetic_share"] == synthetic / 4000


def test_duration_weighting_does_not_count_provider_windows_as_votes():
    result = build_analysis([window(0, 6000)] + [window(i, i + 100, "non-synthetic")
                                               for i in range(6000, 8000, 100)])
    assert result["alert"] == "ai_caller"
    assert result["synthetic_share"] == .75


def test_confidence_is_not_synthetic_share_and_no_content_is_excluded():
    result = build_analysis([window(0, 4000, confidence=.83), window(4000, 12000, "no-content")])
    assert result["synthetic_share"] == 1
    assert result["windows"][0]["confidence"] == .83
    assert result["analyzed_ms"] == 4000
    assert result["no_content_ms"] == 8000


def test_overlapping_windows_count_each_millisecond_once_and_conflicts_are_uncertain():
    result = build_analysis([window(0, 4000), window(0, 4000), window(2000, 6000, "non-synthetic")])
    assert result["synthetic_ms"] == 2000
    assert result["uncertain_ms"] == 2000
    assert result["non_synthetic_ms"] == 2000
    assert result["analyzed_ms"] == 6000
    assert result["alert"] == "inconclusive"


def test_weak_overlapping_evidence_is_uncertain_even_when_other_window_is_confident():
    result = build_analysis([window(0, 4000), window(2000, 6000, confidence=.7)])
    assert result["synthetic_ms"] == 2000
    assert result["uncertain_ms"] == 4000
    assert result["alert"] == "inconclusive"


@pytest.mark.parametrize("uncertain,expected", [(1000, "inconclusive"), (999, "none")])
def test_under_half_abstains_if_uncertain_time_can_reach_half(uncertain, expected):
    result = build_analysis([window(0, 4000), window(4000, 9000, "non-synthetic"),
                             window(9000, 9000 + uncertain, confidence=.6)])
    assert result["alert"] == expected


def test_less_than_four_seconds_reliable_evidence_cannot_alert():
    result = build_analysis([window(0, 3999), window(3999, 20000, confidence=.1)])
    assert result["alert"] == "inconclusive"


def test_live_partial_coverage_can_provisionally_alert():
    result = build_analysis([window(0, 4000)], complete=False)
    assert result["alert"] == "ai_caller"
    assert result["complete"] is False


def test_unknown_epoch_alignment_never_double_counts_or_establishes_verdict():
    result = build_analysis([window(0, 4000), window(0, 4000, stream="MZ-reconnect")])
    assert result["alert"] == "inconclusive"
    assert result["complete"] is False
    assert result["analyzed_ms"] == 4000
    assert result["uncertain_ms"] == 4000


def test_empty_and_no_content_only_have_no_synthetic_share():
    for windows in ([], [window(0, 4000, "no-content")]):
        result = build_analysis(windows)
        assert result["alert"] == "inconclusive"
        assert result["synthetic_share"] is None
        assert result["analyzed_ms"] == 0


@pytest.mark.parametrize("change", [{"alert": "none"}, {"synthetic_ms": 99}, {"version": True},
                                    {"synthetic_share": .83}, {"analyzed_ms": True},
                                    {"track": "outbound"}, {"api_key": "secret"}])
def test_stored_aggregates_must_match_validated_windows(change):
    value = build_analysis([window(0, 4000)])
    with pytest.raises(ValueError):
        validate_analysis(value | change)


@pytest.mark.parametrize("change", [{"start_ms": -1}, {"end_ms": 3_600_001}, {"start_ms": True},
                                    {"confidence": float("nan")}, {"confidence": True},
                                    {"stream_id": "../secret"}, {"verdict": "human"}, {"raw": "audio"}])
def test_invalid_window_values_are_rejected(change):
    with pytest.raises(ValueError):
        build_analysis([window(0, 4000) | change])


def test_window_count_is_bounded():
    with pytest.raises(ValueError):
        build_analysis([window(0, 4000)] * (MAX_WINDOWS + 1))
