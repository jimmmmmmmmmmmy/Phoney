"""Conservative call-level decisions from provider verdict-confidence evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Iterable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from .live import LiveDetectionOutcome


DecisionLabel = Literal["non-synthetic", "synthetic", "unknown"]
MIN_QUALIFIED_AUDIO_MS = 4_000  # Project policy, not a calibrated provider guarantee.


@dataclass(frozen=True, slots=True)
class DetectionDecision:
    """Non-actionable shadow verdict with bounded aggregate metadata."""

    session_id: str | None
    label: DecisionLabel
    confidence: float | None
    reason: str
    streams: int
    observations: int
    accepted_frames: int
    dropped_frames: int
    submitted_audio_ms: int
    coverage_limited: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def decide_call_detection(
    outcomes: Iterable[LiveDetectionOutcome], *, min_confidence: float
) -> DetectionDecision:
    """Summarize completed streams without treating confidence as probability."""
    if (not isinstance(min_confidence, (int, float)) or isinstance(min_confidence, bool)
            or not math.isfinite(min_confidence) or not 0.5 <= min_confidence <= 1):
        raise ValueError("min_confidence must be between 0.5 and 1")
    completed = tuple(outcomes)
    sessions = {item.report.session_id for item in completed if item.report.session_id is not None}
    observations = tuple(observation for item in completed if item.report.reason is None
                         for observation in item.report.observations)
    qualified = tuple(observation for observation in observations
                      if observation.verdict in {"synthetic", "non-synthetic"}
                      and observation.confidence >= min_confidence)
    decisive_observations = tuple(observation for observation in observations
                                  if observation.verdict in {"synthetic", "non-synthetic"})
    verdicts = {observation.verdict for observation in qualified}
    # Overlapping provider windows must not multiply the amount of evidence.
    qualified_ms = 0
    intervals: dict[str, list[tuple[int, int]]] = {}
    for observation in qualified:
        intervals.setdefault(observation.stream_id, []).append((observation.start_ms, observation.end_ms))
    for stream_intervals in intervals.values():
        previous_end = -1
        for start, end in sorted(stream_intervals):
            qualified_ms += max(0, end - max(start, previous_end))
            previous_end = max(previous_end, end)
    dropped_frames = sum(item.dropped_frames for item in completed)
    provider_incomplete = any(item.report.reason not in {None, "no_usable_content"}
                              for item in completed)
    label: DecisionLabel = "unknown"
    reason = "insufficient_evidence" if completed else "no_results"
    confidence = None
    if len(sessions) > 1:
        reason = "mixed_sessions"
    elif provider_incomplete:
        reason = "provider_incomplete"
    elif dropped_frames:
        reason = "dropped_audio"
    elif len(verdicts) > 1:
        reason = "conflicting_evidence"
    elif len(verdicts) == 1 and qualified_ms >= MIN_QUALIFIED_AUDIO_MS:
        provider_verdict = next(iter(verdicts))
        label = provider_verdict
        reason = "confident_non_synthetic" if label == "non-synthetic" else "confident_synthetic"
        confidence = min(observation.confidence for observation in qualified)
    elif qualified:
        reason = "insufficient_evidence"
    elif decisive_observations:
        reason = "below_confidence_threshold"
    elif any(item.report.reason == "no_usable_content" for item in completed):
        reason = "no_usable_content"
    return DetectionDecision(
        session_id=next(iter(sessions)) if len(sessions) == 1 else None,
        label=label,
        confidence=confidence,
        reason=reason,
        streams=len(completed),
        observations=len(observations),
        accepted_frames=sum(item.accepted_frames for item in completed),
        dropped_frames=dropped_frames,
        submitted_audio_ms=sum(item.report.submitted_audio_ms for item in completed),
        coverage_limited=any(item.report.coverage_limited for item in completed),
    )
