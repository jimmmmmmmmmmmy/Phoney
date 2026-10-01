"""Shared fixtures and fakes for focused integration checks."""

CALL = "CA" + "1" * 32


START = "2026-09-26T10:00:00+00:00"


END = "2026-09-26T10:00:30+00:00"


NUMBER = "+12025550101"


def session(call_sid=CALL):
    return {"call_sid": call_sid, "stream_sid": "MZ" + "3" * 32, "started_at": START,
            "ended_at": END, "status": "completed", "segments": [
                {"id": "inbound-0", "track": "inbound", "start_ms": 200, "end_ms": 1800,
                 "text": "Please call back tomorrow.", "confidence": .95}],
            "tracks": {"inbound": {"interim": ""}, "outbound": {"interim": ""}}}
