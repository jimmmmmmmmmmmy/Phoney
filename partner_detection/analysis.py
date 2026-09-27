"""Duration-weighted caller evidence; shares are not provider confidence scores."""

from collections import defaultdict
import math
import re

MAX_WINDOWS = 4096
MAX_TIME_MS = 3_600_000
MIN_RELIABLE_MS = 4000
WINDOW_FIELDS = {"stream_id", "start_ms", "end_ms", "verdict", "confidence"}
ANALYSIS_FIELDS = {"version", "track", "source", "complete", "alert", "synthetic_ms",
                   "non_synthetic_ms", "uncertain_ms", "no_content_ms", "analyzed_ms",
                   "synthetic_share", "min_confidence", "recording_fingerprint", "windows"}
STREAM_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


def validate_windows(windows):
    if not isinstance(windows, (list, tuple)) or len(windows) > MAX_WINDOWS:
        raise ValueError("Detection windows exceed the bounded schema")
    normalized = []
    for window in windows:
        if not isinstance(window, dict) or set(window) != WINDOW_FIELDS:
            raise ValueError("Invalid detection window")
        stream, start, end = window["stream_id"], window["start_ms"], window["end_ms"]
        verdict, confidence = window["verdict"], window["confidence"]
        if (not isinstance(stream, str) or not STREAM_ID.fullmatch(stream)
                or type(start) is not int or type(end) is not int or not 0 <= start < end <= MAX_TIME_MS
                or not isinstance(verdict, str) or verdict not in {"synthetic", "non-synthetic", "no-content"}
                or type(confidence) not in (int, float) or not math.isfinite(confidence)
                or not 0 <= confidence <= 1):
            raise ValueError("Invalid detection window values")
        normalized.append(dict(stream_id=stream, start_ms=start, end_ms=end,
                               verdict=verdict, confidence=float(confidence)))
    return sorted(normalized, key=lambda item: (item["stream_id"], item["start_ms"], item["end_ms"],
                                                item["verdict"], item["confidence"]))


def build_analysis(windows, *, min_confidence=.8, source="live", complete=True,
                   recording_fingerprint=None, version=2):
    """Partition observed time once, preserving qualified speech in overlapping windows.

    Missing intervals are unobserved, not negative evidence. ``complete`` describes
    coverage; provisional alerts may still be useful while it is false. Different
    stream clocks cannot be combined without an explicit common recording origin.
    Version 1 retains the historical overlap veto for validating stored evidence.
    Version 2 lets qualified speech outrank weak speech and no-content windows;
    disagreement between qualified speech verdicts remains uncertain.
    """
    if (type(version) is not int or version not in {1, 2}
            or type(min_confidence) not in (int, float) or not math.isfinite(min_confidence)
            or not .5 <= min_confidence <= 1 or source not in {"live", "recording", "combined"}
            or type(complete) is not bool or (recording_fingerprint is not None
            and (not isinstance(recording_fingerprint, str) or not FINGERPRINT.fullmatch(recording_fingerprint)))):
        raise ValueError("Invalid detection analysis options")
    normalized = validate_windows(windows)
    multiple_epochs = len({window["stream_id"] for window in normalized}) > 1
    events = defaultdict(list)
    for window in normalized:
        verdict = window["verdict"]
        category = ("no_content" if verdict == "no-content" else
                    "uncertain" if multiple_epochs or window["confidence"] < min_confidence else
                    "synthetic" if verdict == "synthetic" else "non_synthetic")
        events[window["start_ms"]].append((category, 1))
        events[window["end_ms"]].append((category, -1))
    totals = {key: 0 for key in ("synthetic", "non_synthetic", "uncertain", "no_content")}
    active = defaultdict(int)
    previous = None
    for timestamp in sorted(events):
        categories = {key for key, count in active.items() if count > 0}
        if previous is not None and categories:
            if version == 1:
                category = next(iter(categories)) if len(categories) == 1 else "uncertain"
            else:
                qualified = categories & {"synthetic", "non_synthetic"}
                if len(qualified) == 1:
                    category = next(iter(qualified))
                elif qualified or "uncertain" in categories:
                    category = "uncertain"
                else:
                    category = "no_content"
            totals[category] += timestamp - previous
        for category, delta in events[timestamp]:
            active[category] += delta
        previous = timestamp
    synthetic, natural, uncertain = (totals[key] for key in ("synthetic", "non_synthetic", "uncertain"))
    analyzed = synthetic + natural + uncertain
    share = synthetic / analyzed if analyzed else None
    alert = "inconclusive"
    if not multiple_epochs and synthetic + natural >= MIN_RELIABLE_MS and analyzed:
        # Integer comparisons preserve the exact 50% and 75% boundaries.
        if synthetic * 4 >= analyzed * 3:
            alert = "ai_caller"
        elif synthetic * 2 >= analyzed:
            alert = "potential_ai"
        elif (synthetic + uncertain) * 2 < analyzed:
            alert = "none"
    return {
        "version": version, "track": "inbound", "source": source,
        "complete": complete and not multiple_epochs, "alert": alert,
        "synthetic_ms": synthetic, "non_synthetic_ms": natural,
        "uncertain_ms": uncertain, "no_content_ms": totals["no_content"],
        "analyzed_ms": analyzed, "synthetic_share": share,
        "min_confidence": float(min_confidence), "recording_fingerprint": recording_fingerprint,
        "windows": normalized,
    }


def validate_analysis(value):
    if (not isinstance(value, dict) or set(value) != ANALYSIS_FIELDS
            or type(value["version"]) is not int or value["version"] not in {1, 2}):
        raise ValueError("Invalid detection analysis")
    rebuilt = build_analysis(value["windows"], min_confidence=value["min_confidence"],
                             source=value["source"], complete=value["complete"],
                             recording_fingerprint=value["recording_fingerprint"],
                             version=value["version"])
    if (any(type(value[key]) is not int for key in
                   ("synthetic_ms", "non_synthetic_ms", "uncertain_ms", "no_content_ms", "analyzed_ms"))
            or type(value["complete"]) is not bool
            or (value["synthetic_share"] is not None and type(value["synthetic_share"]) not in (int, float))
            or any(value[key] != rebuilt[key] for key in ANALYSIS_FIELDS - {"windows"})):
        raise ValueError("Detection analysis does not match its evidence")
    return rebuilt
