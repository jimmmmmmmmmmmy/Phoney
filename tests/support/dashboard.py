"""Shared fixtures and fakes for focused integration checks."""

from copy import deepcopy


from fastapi import FastAPI


from fastapi.testclient import TestClient


from config import Settings


from dashboard import register_dashboard


SID = "CA" + "a" * 32


OTHER = "CA" + "b" * 32


SETTINGS = Settings("AC" + "1" * 32, "secret-auth", "https://operator.example",
                    deepgram_api_key="never-in-browser")


SAMPLE = {"enabled": True, "provider": "deepgram", "model": "nova-3", "revision": 3,
          "sessions": [
              {"call_sid": SID, "stream_sid": "MZ" + "a" * 32,
               "started_at": "2026-09-26T12:00:00Z", "ended_at": None, "status": "live",
               "tracks": {"inbound": {"status": "live", "error": None, "interim": "Hello"},
                          "outbound": {"status": "live", "error": None, "interim": ""}},
               "segments": [{"id": 1, "track": "inbound", "start_ms": 1100, "end_ms": 2000,
                             "text": "<script>not executable</script>", "confidence": .9}]},
              {"call_sid": OTHER, "stream_sid": "MZ" + "b" * 32,
               "started_at": "2026-09-26T11:00:00Z", "ended_at": "2026-09-26T11:01:00Z",
               "status": "completed", "model": "nova-2", "tracks": {},
               "segments": [{"id": 1, "track": "outbound", "start_ms": 0, "end_ms": 1000,
                             "text": "An earlier call", "confidence": .8}]}]}


class Manager:
    def snapshot(self):
        return deepcopy(SAMPLE)


def client_for(settings=SETTINGS, voicemail_store=None, manager=None):
    app = FastAPI()
    register_dashboard(app, settings, manager or Manager(), voicemail_store)
    return TestClient(app, base_url="https://operator.example")
