"""Shared fixtures and fakes for focused integration checks."""

from partner_detection import DetectionObservation, DetectionReport, LiveDetectionOutcome, decide_call_detection


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
