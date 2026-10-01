"""Shared fixtures and fakes for focused integration checks."""

import asyncio


from fastapi.testclient import TestClient


from app import create_app


from call_details import CallDetailsStore


from config import Settings


from transcription import storage


SID = "CA" + "a" * 32


DOCUMENT = {"call_sid": SID, "started_at": "2026-09-26T12:00:00+00:00",
            "schema_version": 1, "provider": "deepgram", "model": "nova-3",
            "stream_sid": "MZ" + "b" * 32, "finish_reason": "", "storage_error": "",
            "ended_at": "2026-09-26T12:00:20+00:00", "status": "completed",
            "tracks": {name: {"meaning": meaning, "status": "completed", "error": "", "interim": ""}
                       for name, meaning in (("inbound", "caller-input"), ("outbound", "caller-playback"))},
            "segments": [{"track": "inbound", "start_ms": 0, "end_ms": 5000,
                          "text": "Please call tomorrow.", "id": "in-1", "confidence": .9}]}


class Provider:
    def __init__(self):
        self.calls = 0
        self.brief_calls = 0
        self.closed = False

    async def summarize(self, document):
        self.calls += 1
        return "Caller requested a callback tomorrow."

    async def summarize_brief(self, document):
        self.brief_calls += 1
        return "Caller requested a callback tomorrow."

    async def close(self):
        self.closed = True


def settings(tmp_path):
    return Settings("AC" + "1" * 32, "test-auth", "https://operator.example",
        media_capture_enabled=True, media_storage_dir=str(tmp_path / "audio"),
        transcription_enabled=True, transcript_storage_dir=str(tmp_path / "transcripts"),
        deepgram_api_key="test-deepgram", call_details_storage_dir=str(tmp_path / "details"),
        gemini_api_key="private-gemini-test-key", deploy_control_token="private-control-" * 3)
