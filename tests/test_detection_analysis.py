"""Duration weighting, uncertainty, and threshold boundaries are explicit policy."""

import pytest

from partner_detection.analysis import build_analysis, validate_analysis, MAX_WINDOWS


def window(start, end, verdict="synthetic", confidence=.94, stream="MZ-one"):
    return dict(stream_id=stream, start_ms=start, end_ms=end, verdict=verdict, confidence=confidence)


@pytest.mark.parametrize("synthetic, expected", [(1999, "none"), (2000, "potential_ai"),
                                                 (2999, "potential_ai"), (3000, "ai_caller"),
                                                 (4000, "ai_caller")])
@pytest.mark.parametrize("version", [1, 2])
def test_legacy_exact_duration_threshold_boundaries(synthetic, expected, version):
    windows = [window(0, synthetic)]
    if synthetic < 4000:
        windows.append(window(synthetic, 4000, "non-synthetic"))
    result = build_analysis(windows, version=version)
    assert result["alert"] == expected
    assert result["synthetic_ms"] == synthetic
    assert result["analyzed_ms"] == 4000
    assert result["synthetic_share"] == synthetic / 4000


def test_duration_weighting_does_not_count_provider_windows_as_votes():
    result = build_analysis([window(0, 6000)] + [window(i, i + 100, "non-synthetic")
                                               for i in range(6000, 8000, 100)])
    assert result["alert"] == "ai_detected"
    assert result["synthetic_share"] == .75


@pytest.mark.parametrize("synthetic,expected", [(1, "inconclusive"), (3999, "inconclusive"),
                                               (4000, "ai_detected"), (4001, "ai_detected")])
def test_brief_synthetic_speech_in_a_long_human_call_uses_evidence_duration(synthetic, expected):
    result = build_analysis([window(0, 60000, "non-synthetic"), window(60000, 60000 + synthetic),
                             window(60000 + synthetic, 600000, "non-synthetic")])
    assert result["version"] == 3
    assert result["alert"] == expected
    assert result["synthetic_ms"] == synthetic
    assert result["synthetic_share"] == synthetic / 600000
    assert result["synthetic_intervals"] == [dict(stream_id="MZ-one", start_ms=60000, end_ms=60000 + synthetic)]


def test_disjoint_synthetic_intervals_sum_to_gate_without_coloring_the_gap():
    result = build_analysis([window(0, 2000), window(2000, 8000, "non-synthetic"), window(8000, 10000)])
    assert result["alert"] == "ai_detected"
    assert result["synthetic_intervals"] == [dict(stream_id="MZ-one", start_ms=0, end_ms=2000),
                                             dict(stream_id="MZ-one", start_ms=8000, end_ms=10000)]


def test_synthetic_intervals_merge_overlaps_and_adjacent_evidence_but_exclude_conflicts():
    result = build_analysis([window(0, 8000), window(1000, 6000), window(8000, 10000),
                             window(3000, 5000, "non-synthetic"), window(0, 10000, confidence=.6),
                             window(0, 10000, "no-content")])
    assert result["synthetic_intervals"] == [dict(stream_id="MZ-one", start_ms=0, end_ms=3000),
                                             dict(stream_id="MZ-one", start_ms=5000, end_ms=10000)]
    assert result["synthetic_ms"] == 8000
    assert result["uncertain_ms"] == 2000
    assert sum(span["end_ms"] - span["start_ms"] for span in result["synthetic_intervals"]) == result["synthetic_ms"]


@pytest.mark.parametrize("uncertain,expected", [(3999, "none"), (4000, "inconclusive")])
def test_natural_diagnostic_requires_no_synthetic_and_insufficient_uncertainty_to_alert(uncertain, expected):
    result = build_analysis([window(0, 10000, "non-synthetic"),
                             window(10000, 10000 + uncertain, confidence=.5)])
    assert result["alert"] == expected
    assert result["synthetic_intervals"] == []


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


@pytest.mark.parametrize("weak_verdict", ["synthetic", "non-synthetic"])
@pytest.mark.parametrize("strong_verdict", ["synthetic", "non-synthetic"])
def test_qualified_speech_takes_precedence_over_weak_overlapping_evidence(weak_verdict, strong_verdict):
    result = build_analysis([window(0, 4000, strong_verdict),
                             window(2000, 6000, weak_verdict, confidence=.7)])
    assert result[strong_verdict.replace("-", "_") + "_ms"] == 4000
    assert result["uncertain_ms"] == 2000
    assert result["analyzed_ms"] == 6000
    assert result["alert"] == ("ai_detected" if strong_verdict == "synthetic" else "none")


@pytest.mark.parametrize("verdict", ["synthetic", "non-synthetic"])
def test_no_content_overlap_never_vetoes_qualified_speech(verdict):
    result = build_analysis([window(0, 8000, "no-content", confidence=1),
                             window(2000, 6000, verdict)])
    assert result[verdict.replace("-", "_") + "_ms"] == 4000
    assert result["no_content_ms"] == 4000
    assert result["uncertain_ms"] == 0
    assert result["analyzed_ms"] == 4000
    assert result["alert"] == ("ai_detected" if verdict == "synthetic" else "none")


def test_weak_speech_over_no_content_stays_uncertain_and_in_denominator():
    result = build_analysis([window(0, 4000), window(4000, 8000, confidence=.7),
                             window(4000, 10000, "no-content", confidence=1)])
    assert result["synthetic_ms"] == 4000
    assert result["uncertain_ms"] == 4000
    assert result["no_content_ms"] == 2000
    assert result["analyzed_ms"] == 8000
    assert result["synthetic_share"] == .5
    assert result["alert"] == "ai_detected"


def test_conflicting_qualified_speech_remains_uncertain_despite_other_windows():
    result = build_analysis([window(0, 4000), window(0, 4000, "non-synthetic"),
                             window(0, 4000, confidence=.7), window(0, 4000, "no-content")])
    assert result["uncertain_ms"] == 4000
    assert result["synthetic_ms"] == result["non_synthetic_ms"] == result["no_content_ms"] == 0
    assert result["analyzed_ms"] == 4000
    assert result["alert"] == "inconclusive"


def test_rolling_windows_preserve_strong_evidence_without_double_counting():
    result = build_analysis([window(0, 4000), window(1000, 5000, confidence=.7),
                             window(2000, 6000), window(3000, 7000, "no-content")])
    assert result["synthetic_ms"] == result["analyzed_ms"] == 6000
    assert result["no_content_ms"] == 1000
    assert result["uncertain_ms"] == 0
    assert result["alert"] == "ai_detected"


@pytest.mark.parametrize("confidence,expected", [(.799999, "inconclusive"), (.8, "ai_detected")])
def test_confidence_boundary_applies_before_overlap_precedence(confidence, expected):
    result = build_analysis([window(0, 4000, confidence=confidence),
                             window(0, 4000, "no-content")])
    assert result["alert"] == expected
    assert result["analyzed_ms"] == 4000


def legacy_analysis():
    return {"version": 1, "track": "inbound", "source": "live", "complete": True,
            "alert": "inconclusive", "synthetic_ms": 2000, "non_synthetic_ms": 0,
            "uncertain_ms": 4000, "no_content_ms": 0, "analyzed_ms": 6000,
            "synthetic_share": 1 / 3, "min_confidence": .8, "recording_fingerprint": None,
            "windows": [window(0, 4000), window(2000, 6000, confidence=.7)]}


def test_legacy_analysis_is_strictly_validated_without_rewriting_its_policy():
    legacy = legacy_analysis()
    assert validate_analysis(legacy) == legacy
    assert build_analysis(legacy["windows"], version=1) == legacy
    current = build_analysis(legacy["windows"])
    assert current["version"] == 3
    assert current["synthetic_ms"] == 4000
    assert validate_analysis(current) == current


def test_version_two_preserves_old_thresholds_and_exact_field_set():
    windows = [window(0, 4000), window(4000, 60000, "non-synthetic")]
    old = build_analysis(windows, version=2)
    assert old["alert"] == "none" and "synthetic_intervals" not in old
    assert validate_analysis(old) == old
    current = build_analysis(windows)
    assert current["alert"] == "ai_detected"
    with pytest.raises(ValueError):
        validate_analysis(old | {"version": 3})
    with pytest.raises(ValueError):
        validate_analysis(current | {"version": 2})
    with pytest.raises(ValueError):
        validate_analysis(old | {"synthetic_intervals": []})


@pytest.mark.parametrize("change", [{"version": 2}, {"synthetic_ms": 4000},
                                    {"alert": "potential_ai"}, {"uncertain_ms": 2000}])
def test_legacy_evidence_cannot_be_relabelled_or_given_new_aggregates(change):
    with pytest.raises(ValueError):
        validate_analysis(legacy_analysis() | change)


@pytest.mark.parametrize("version", [True, False, 0, 4, "3", 3.0, None])
def test_only_known_integer_analysis_versions_are_accepted(version):
    with pytest.raises(ValueError):
        build_analysis([], version=version)
    with pytest.raises(ValueError):
        validate_analysis(build_analysis([]) | {"version": version})


@pytest.mark.parametrize("uncertain,expected", [(1000, "inconclusive"), (999, "none")])
def test_under_half_abstains_if_uncertain_time_can_reach_half(uncertain, expected):
    result = build_analysis([window(0, 4000), window(4000, 9000, "non-synthetic"),
                             window(9000, 9000 + uncertain, confidence=.6)], version=2)
    assert result["alert"] == expected


def test_less_than_four_seconds_reliable_evidence_cannot_alert():
    result = build_analysis([window(0, 3999), window(3999, 20000, confidence=.1)])
    assert result["alert"] == "inconclusive"


def test_live_partial_coverage_can_provisionally_alert():
    result = build_analysis([window(0, 4000)], complete=False)
    assert result["alert"] == "ai_detected"
    assert result["complete"] is False


def test_unknown_epoch_alignment_never_double_counts_or_establishes_verdict():
    result = build_analysis([window(0, 4000), window(0, 4000, stream="MZ-reconnect")])
    assert result["alert"] == "inconclusive"
    assert result["complete"] is False
    assert result["analyzed_ms"] == 4000
    assert result["uncertain_ms"] == 4000
    assert result["synthetic_intervals"] == []


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


@pytest.mark.parametrize("intervals", [[], [{"stream_id": "MZ-one", "start_ms": False, "end_ms": 4000}],
    [{"stream_id": "MZ-other", "start_ms": 0, "end_ms": 4000}],
    [{"stream_id": "MZ-one", "start_ms": 0, "end_ms": 4001}],
    [{"stream_id": "MZ-one", "start_ms": 0, "end_ms": 4000, "raw": "private"}],
    [{"stream_id": "MZ-one", "start_ms": 0, "end_ms": 2000},
     {"stream_id": "MZ-one", "start_ms": 2000, "end_ms": 4000}],
    None, "invalid"])
def test_synthetic_intervals_must_exactly_match_recomputed_evidence(intervals):
    value = build_analysis([window(0, 4000)])
    with pytest.raises(ValueError):
        validate_analysis(value | {"synthetic_intervals": intervals})


@pytest.mark.parametrize("change", [{"start_ms": -1}, {"end_ms": 3_600_001}, {"start_ms": True},
                                    {"confidence": float("nan")}, {"confidence": True},
                                    {"stream_id": "../secret"}, {"verdict": "human"}, {"raw": "audio"}])
def test_invalid_window_values_are_rejected(change):
    with pytest.raises(ValueError):
        build_analysis([window(0, 4000) | change])


def test_window_count_is_bounded():
    with pytest.raises(ValueError):
        build_analysis([window(0, 4000)] * (MAX_WINDOWS + 1))
