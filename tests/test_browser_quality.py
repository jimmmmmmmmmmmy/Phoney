"""Quality reporting proves ownership and cannot become an audio/token sink."""

import os
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_service.browser_quality import BrowserQualityStore, register_browser_quality_routes


OWNER_SID = "CA" + "1" * 32
REMOTE_SID = "CA" + "2" * 32
SESSION_ID = "a" * 32
HEADERS = {"Origin": "https://phoney.example", "X-Agent-Request": "1", "X-Test-Owner": "1"}


def report(sequence=1, *, final=False):
    return {"version": 1, "sequence": sequence, "final": final, "elapsed_ms": 10000,
        "codec": "opus", "device": {"browser": "safari", "platform": "mac",
            "sdk_version": "2.18.5", "sample_rate": 48000, "channel_count": 1,
            "audio_track_count": 1, "echo_cancellation": True},
        "samples": [{"elapsed_ms": 9000, "jitter": 42, "rtt": 510,
                     "mos": 2.8, "packetsLostFraction": 7.5, "packetsLost": 4}],
        "warnings": [{"elapsed_ms": 9100, "name": "high-jitter", "cleared": False}]}


def route_client(tmp_path, *, owner=True):
    settings = SimpleNamespace(call_details_storage_dir=str(tmp_path),
                               public_base_url="https://phoney.example")
    session = SimpleNamespace(id=SESSION_ID, browser_audio=True, active=True, ended_at=None,
        canonical_call_sid=REMOTE_SID,
        legs={"owner": SimpleNamespace(transport="sdk", call_sid=OWNER_SID),
              "remote": SimpleNamespace(call_sid=REMOTE_SID)})
    sessions = SimpleNamespace(find=lambda sid: session if sid == SESSION_ID else None)
    app = FastAPI()
    store = register_browser_quality_routes(app, settings, sessions,
        require_owner=lambda request: owner and request.headers.get("x-test-owner") == "1")
    client = TestClient(app, base_url=settings.public_base_url)
    return client, session, store


def test_owner_quality_is_history_linked_private_restart_safe_and_fail_soft(tmp_path, monkeypatch):
    client, session, store = route_client(tmp_path)
    path = f"/api/sessions/{SESSION_ID}/browser-quality"
    assert client.post(path, headers=HEADERS, json=report(final=True)).status_code == 204
    assert store.load(OWNER_SID) is None
    saved = store.load(REMOTE_SID)
    assert saved["owner_call_sid"] == OWNER_SID and saved["session_id"] == session.id
    assert saved["codec"] == "opus" and saved["final"] is True
    assert saved["metrics"]["packetsLostFraction"]["mean"] == 7.5
    assert saved["warning_counts"]["high-jitter:raised"] == 1
    assert saved["device"]["sample_rate"] == 48000
    assert os.stat(store.path).st_mode & 0o777 == 0o700
    assert os.stat(store.path / (REMOTE_SID + ".json")).st_mode & 0o777 == 0o600
    restarted = BrowserQualityStore(SimpleNamespace(call_details_storage_dir=str(tmp_path)))
    assert restarted.load(REMOTE_SID) == saved
    def unavailable(*args):
        raise OSError("Disk unavailable")
    monkeypatch.setattr(store, "record", unavailable)
    assert client.post(path, headers=HEADERS, json=report(2, final=True)).status_code == 204
    assert restarted.load(REMOTE_SID) == saved


def test_quality_rejects_public_cross_origin_unbound_sensitive_and_excess_reports(tmp_path):
    client, session, store = route_client(tmp_path)
    path = f"/api/sessions/{SESSION_ID}/browser-quality"
    assert client.post(path, json=report()).status_code == 403
    assert client.post(path, headers={**HEADERS, "X-Test-Owner": "0"}, json=report()).status_code == 403
    assert client.post(path, headers={**HEADERS, "Origin": "https://evil.example"}, json=report()).status_code == 403
    assert client.post(path, headers={**HEADERS, "X-Agent-Request": "0"}, json=report()).status_code == 403
    assert client.post("/api/sessions/" + "b" * 32 + "/browser-quality", headers=HEADERS, json=report()).status_code == 404
    session.legs["owner"].call_sid = ""
    assert client.post(path, headers=HEADERS, json=report()).status_code == 404
    session.legs["owner"].call_sid = OWNER_SID
    session.legs["owner"].transport = "phone"
    assert client.post(path, headers=HEADERS, json=report()).status_code == 404
    assert not store.path.exists()
    session.legs["owner"].transport = "sdk"
    payload = report()
    payload["samples"][0]["token"] = "private-token"
    assert client.post(path, headers=HEADERS, json=payload).status_code == 400
    payload = report()
    payload["call_sid"] = OWNER_SID
    assert client.post(path, headers=HEADERS, json=payload).status_code == 400
    assert client.post(path, headers=HEADERS, content="x" * 16001).status_code == 413
    assert not store.path.exists()
    assert client.post(path, headers=HEADERS, json=report()).status_code == 204
    assert client.post(path, headers=HEADERS, json=report(2)).status_code == 429


def test_quality_final_flush_survives_disconnect_and_is_idempotent_with_limited_grace(tmp_path):
    client, session, store = route_client(tmp_path)
    path = f"/api/sessions/{SESSION_ID}/browser-quality"
    assert client.post(path, headers=HEADERS, json=report()).status_code == 204
    session.active = False
    session.ended_at = time.monotonic()
    assert client.post(path, headers=HEADERS, json=report()).status_code == 409
    assert client.post(path, headers=HEADERS, json=report(2, final=True)).status_code == 204
    saved = store.load(REMOTE_SID)
    assert saved["sample_count"] == 2 and saved["final"]
    assert client.post(path, headers=HEADERS, json=report(2, final=True)).status_code == 204
    assert store.load(REMOTE_SID) == saved
    session.ended_at -= 121
    assert client.post(path, headers=HEADERS, json=report(2, final=True)).status_code == 409
