"""Viewer authentication and partner exports, with no call or provider requests."""

from copy import deepcopy
from dataclasses import replace
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from config import Settings
from dashboard import COOKIE, _session_cookie, register_dashboard


SID = "CA" + "a" * 32
OTHER = "CA" + "b" * 32
SETTINGS = Settings("AC" + "1" * 32, "secret-auth", "https://operator.example",
                    dashboard_token="test-viewer-" * 4, deepgram_api_key="never-in-browser")
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
               "status": "completed", "tracks": {},
               "segments": [{"id": 1, "track": "outbound", "start_ms": 0, "end_ms": 1000,
                             "text": "An earlier call", "confidence": .8}]}]}


class Manager:
    def snapshot(self):
        return deepcopy(SAMPLE)


def client_for(settings=SETTINGS):
    app = FastAPI()
    register_dashboard(app, settings, Manager())
    return TestClient(app, base_url="https://operator.example")


def login(client):
    return client.post("/dashboard/login", json={"token": SETTINGS.dashboard_token},
                       headers={"Origin": SETTINGS.public_base_url})


def test_private_api_requires_viewer_and_does_not_accept_query_token():
    with client_for() as client:
        assert client.get("/api/transcripts").status_code == 401
        assert client.get("/api/transcripts", params={"token": SETTINGS.dashboard_token}).status_code == 401
        assert client.get(f"/api/transcripts/{SID}/export").status_code == 401
        assert client.get("/api/transcripts", headers={"Authorization": "Bearer deploy-token"}).status_code == 401


def test_login_cookie_security_and_expiry():
    with client_for() as client:
        response = login(client)
        assert response.status_code == 200
        assert all(flag in response.headers["set-cookie"] for flag in ("HttpOnly", "Secure", "SameSite=strict"))
        assert SETTINGS.dashboard_token not in response.headers["set-cookie"]
        assert client.get("/api/transcripts").status_code == 200
        client.cookies.clear()
        client.cookies.set(COOKIE, _session_cookie(SETTINGS.dashboard_token, int(time.time()) - 1))
        assert client.get("/api/transcripts").status_code == 401
        client.cookies.set(COOKIE, "viewer-v1.9999999999999.not-a-signature")
        assert client.get("/api/transcripts").status_code == 401


def test_login_origin_body_bounds_failure_limit_and_logout():
    with client_for() as client:
        assert client.post("/dashboard/login", json={"token": SETTINGS.dashboard_token},
                           headers={"Origin": "https://other.example"}).status_code == 403
        assert client.post("/dashboard/login", content="x" * 1025).status_code == 413
        assert client.post("/dashboard/login", content="not json").status_code == 400
        assert login(client).status_code == 200
        assert client.post("/dashboard/logout").status_code == 200
        assert client.get("/api/transcripts").status_code == 401
        for _ in range(20):
            assert client.post("/dashboard/login", json={"token": "wrong"}).status_code == 401
        assert client.post("/dashboard/login", json={"token": "wrong"}).status_code == 429
        # A shared limiter must not let anonymous guesses lock out a valid owner.
        assert login(client).status_code == 200


def test_bearer_snapshot_and_selection_only_expose_one_transcript():
    with client_for() as client:
        headers = {"Authorization": "Bearer " + SETTINGS.dashboard_token}
        response = client.get("/api/transcripts", headers=headers)
        data = response.json()
        assert data["schema_version"] == 1 and data["selected_call_sid"] == SID
        assert data["sessions"][0]["segments"] and not data["sessions"][1]["segments"]
        assert response.headers["cache-control"] == "no-store"
        assert SETTINGS.deepgram_api_key not in response.text and SETTINGS.dashboard_token not in response.text
        selected = client.get("/api/transcripts", params={"call_sid": OTHER}, headers=headers).json()
        assert not selected["sessions"][0]["segments"]
        assert selected["sessions"][0]["tracks"]["inbound"]["interim"] == ""
        assert selected["sessions"][1]["segments"][0]["text"] == "An earlier call"


def test_exports_keep_track_and_time_metadata_and_are_read_only():
    with client_for() as client:
        login(client)
        response = client.get(f"/api/transcripts/{SID}/export?format=json")
        assert response.json()["session"]["segments"][0]["start_ms"] == 1100
        assert response.json()["sample_rate"] == 8000
        assert response.headers["content-type"] == "application/json"
        assert "attachment" in response.headers["content-disposition"]
        text = client.get(f"/api/transcripts/{SID}/export?format=txt")
        assert "[00:01] Caller input: <script>" in text.text
        assert text.headers["content-type"].startswith("text/plain")
        assert client.get(f"/api/transcripts/{SID}/export?format=html").status_code == 400
        assert client.get("/api/transcripts/CA" + "c" * 32 + "/export").status_code == 404
        assert client.post("/api/transcripts", json={"action": "hangup"}).status_code == 405


def test_unconfigured_viewer_fails_closed():
    with client_for(replace(SETTINGS, dashboard_token="")) as client:
        assert client.get("/api/transcripts").status_code == 503
        assert client.post("/dashboard/login", json={"token": ""}).status_code == 503


def test_html_is_public_but_contains_no_call_data_or_keys():
    with client_for() as client:
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert SETTINGS.dashboard_token not in response.text
        assert SETTINGS.deepgram_api_key not in response.text
        assert SID not in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "script-src 'sha256-" in response.headers["content-security-policy"]
        assert response.headers["referrer-policy"] == "no-referrer"
