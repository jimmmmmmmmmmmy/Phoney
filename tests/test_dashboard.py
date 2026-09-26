"""Public transcript views stay read-only and never expose provider credentials."""

import base64
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import create_app
from config import Settings
from dashboard import register_dashboard
from scripts import open_dashboard


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


class VoicemailStore:
    def snapshot(self):
        return {"enabled": True, "storage_error": "", "voicemails": [{
            "schema_version": 1, "call_sid": SID, "mode": "voicemail_stub", "reason": "no-answer",
            "started_at": "2026-09-26T12:00:00Z", "ended_at": "2026-09-26T12:00:12Z",
            "recording_status": "completed", "recording_sid": "RE" + "c" * 32,
            "duration_seconds": 12, "storage_error": ""}]}


def client_for(settings=SETTINGS, voicemail_store=None, manager=None):
    app = FastAPI()
    register_dashboard(app, settings, manager or Manager(), voicemail_store)
    return TestClient(app, base_url="https://operator.example")


def test_url_alone_opens_dashboard_transcripts_and_exports_without_cookies():
    with client_for() as client:
        for path in ("/dashboard", "/api/transcripts", "/api/voicemails", f"/api/transcripts/{SID}/export?format=json",
                     f"/api/transcripts/{SID}/export?format=txt"):
            response = client.get(path)
            assert response.status_code == 200
            assert "set-cookie" not in response.headers
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert SETTINGS.deepgram_api_key not in response.text
            assert SETTINGS.auth_token not in response.text
        assert not client.cookies
        assert client.get("/api/transcripts", headers={"Authorization": "Bearer irrelevant"}).status_code == 200


def test_login_and_logout_routes_are_removed():
    with client_for() as client:
        assert client.post("/dashboard/login", json={"token": "unused"}).status_code == 404
        assert client.post("/dashboard/logout").status_code == 404
        assert client.get("/api/transcripts").status_code == 200


def test_snapshot_selection_only_includes_the_selected_transcript():
    with client_for() as client:
        data = client.get("/api/transcripts").json()
        assert data["schema_version"] == 1 and data["selected_call_sid"] == SID
        assert data["sessions"][0]["segments"] and not data["sessions"][1]["segments"]
        selected = client.get("/api/transcripts", params={"call_sid": OTHER}).json()
        assert not selected["sessions"][0]["segments"]
        assert selected["sessions"][0]["tracks"]["inbound"]["interim"] == ""
        assert selected["sessions"][1]["segments"][0]["text"] == "An earlier call"
        assert client.get("/api/transcripts", params={"call_sid": "missing"}).json()["selected_call_sid"] == SID
        assert SAMPLE["sessions"][0]["segments"]  # Selection cannot mutate the manager's history.


def test_exports_keep_track_time_and_historical_model_metadata():
    with client_for() as client:
        response = client.get(f"/api/transcripts/{SID}/export?format=json")
        assert response.json()["session"]["segments"][0]["start_ms"] == 1100
        assert response.json()["sample_rate"] == 8000
        assert response.headers["content-type"] == "application/json"
        assert "attachment" in response.headers["content-disposition"]
        assert response.json()["track_meanings"]["outbound"] == "caller-playback"
        assert client.get(f"/api/transcripts/{OTHER}/export").json()["model"] == "nova-2"
        text = client.get(f"/api/transcripts/{SID}/export?format=txt")
        assert "[00:01] Caller input: <script>not executable</script>" in text.text
        assert text.headers["content-type"].startswith("text/plain")
        assert text.headers["x-content-type-options"] == "nosniff"
        assert client.get(f"/api/transcripts/{SID}/export?format=html").status_code == 400
        assert client.get("/api/transcripts/CA" + "c" * 32 + "/export").status_code == 404
        assert client.get("/api/transcripts/invalid/export").status_code == 400


def test_public_views_offer_no_write_or_call_control_methods():
    with client_for() as client:
        for method in ("post", "put", "patch", "delete"):
            for path in ("/api/transcripts", "/api/voicemails", f"/api/transcripts/{SID}/export", "/dashboard"):
                assert getattr(client, method)(path).status_code == 405
    # Public transcript access must not loosen the existing switchboard or deploy authentication.
    settings = replace(SETTINGS, deploy_control_token="deploy-control-secret-at-least-32-chars")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/transcripts").status_code == 200
        assert client.get("/internal/deploy").status_code == 403
        assert client.post("/internal/deploy", json={"draining": True}).status_code == 403
        assert client.post("/voice", data={"AccountSid": settings.account_sid, "CallSid": SID}).status_code == 403


def test_voicemail_metadata_is_visible_without_transcription_or_login():
    class DisabledManager:
        def snapshot(self):
            return {"enabled": False, "provider": "deepgram", "model": "nova-3", "revision": 0, "sessions": []}
    store = VoicemailStore()
    with client_for(voicemail_store=store, manager=DisabledManager()) as client:
        snapshot = client.get("/api/transcripts").json()
        assert snapshot["sessions"] == [] and snapshot["enabled"] is False
        assert snapshot["voicemail"] == store.snapshot()
        inbox = client.get("/api/voicemails")
        assert inbox.json() == store.snapshot()
        assert inbox.headers["cache-control"] == "no-store"
        assert "set-cookie" not in inbox.headers
        assert all(private not in inbox.text for private in ("recording_url", "file_path", "phone_number", SETTINGS.deepgram_api_key))
        # A recording does not imply that a transcript exists or an audio endpoint is exposed.
        assert client.get(f"/api/transcripts/{SID}/export").status_code == 404
        assert client.get(f"/api/voicemails/{SID}/audio").status_code == 404


@pytest.mark.parametrize("status", ["awaiting", "recording", "processing", "completed", "absent", "failed"])
def test_voicemail_api_preserves_recording_status_without_claiming_completion(status):
    class Store(VoicemailStore):
        def snapshot(self):
            value = super().snapshot()
            value["voicemails"][0]["recording_status"] = status
            return value
    with client_for(voicemail_store=Store()) as client:
        assert client.get("/api/voicemails").json()["voicemails"][0]["recording_status"] == status
        assert client.get("/api/transcripts").json()["voicemail"]["voicemails"][0]["recording_status"] == status


def test_transcript_exports_include_matching_voicemail_metadata_only():
    with client_for(voicemail_store=VoicemailStore()) as client:
        exported = client.get(f"/api/transcripts/{SID}/export").json()
        assert exported["voicemail"]["recording_status"] == "completed"
        assert exported["voicemail"]["duration_seconds"] == 12
        assert exported["session"]["segments"] == SAMPLE["sessions"][0]["segments"]
        plain = client.get(f"/api/transcripts/{SID}/export?format=txt").text
        assert "Voicemail recording status: completed" in plain
        assert "Voicemail recording duration: 12 seconds" in plain
        assert "voicemail" not in client.get(f"/api/transcripts/{OTHER}/export").json()


def test_html_opens_the_dashboard_directly_and_keeps_csp_safe_text_rendering():
    with client_for() as client:
        response = client.get("/dashboard")
        html = response.text
        assert SID not in html
        assert '<section id="dashboard-view" aria-labelledby=' in html
        assert all(removed not in html for removed in ("access-code", "login-form", "Sign out", "/dashboard/login"))
        assert "innerHTML" not in html
        assert '.textContent = content' in html
        csp = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
        for block in re.findall(r"<(?:script|style)>(.*?)</(?:script|style)>", html, re.S):
            digest = base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()
            assert "'sha256-" + digest + "'" in csp


def test_opener_uses_public_url_without_any_secret(tmp_path, monkeypatch, capsys):
    env = tmp_path / "test.env"
    env.write_text("PUBLIC_BASE_URL=https://operator.example/\nDEEPGRAM_API_KEY=provider-secret\n")
    opened = []
    monkeypatch.setattr("sys.argv", ["open_dashboard.py", "--env-file", str(env)])
    monkeypatch.setattr(open_dashboard.webbrowser, "open", lambda url: opened.append(url) or True)
    open_dashboard.main()
    assert opened == ["https://operator.example/dashboard"]
    assert "provider-secret" not in capsys.readouterr().out


def test_opener_prefers_installed_server_origin(tmp_path, monkeypatch):
    root = tmp_path / "source"
    (root / ".runtime").mkdir(parents=True)
    home = tmp_path / "home"
    installed = home / "Library/Application Support/NewCollegeOperator"
    installed.mkdir(parents=True)
    (root / ".runtime/server-root.json").write_text(json.dumps({"path": str(installed)}))
    (root / ".env").write_text("PUBLIC_BASE_URL=https://old.example\n")
    (installed / ".env").write_text("PUBLIC_BASE_URL=https://installed.example\n")
    opened = []
    monkeypatch.setattr(open_dashboard, "__file__", str(root / "scripts/open_dashboard.py"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("sys.argv", ["open_dashboard.py"])
    monkeypatch.setattr(open_dashboard.webbrowser, "open", lambda url: opened.append(url) or True)
    open_dashboard.main()
    assert opened == ["https://installed.example/dashboard"]


@pytest.mark.parametrize("origin", ["http://operator.example", "https://name:secret@operator.example",
                                    "https://operator.example/path", "https://operator.example?token=secret"])
def test_opener_rejects_origins_with_insecure_or_embedded_credentials(tmp_path, monkeypatch, capsys, origin):
    env = tmp_path / "test.env"
    env.write_text(f"PUBLIC_BASE_URL={origin}\n")
    monkeypatch.setattr("sys.argv", ["open_dashboard.py", "--env-file", str(env)])
    monkeypatch.setattr(open_dashboard.webbrowser, "open", lambda url: pytest.fail("Unexpected browser open"))
    with pytest.raises(SystemExit):
        open_dashboard.main()
    assert "secret" not in capsys.readouterr().err
