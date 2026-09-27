"""The existing read-only dashboard projects separate acoustic advisories."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard import register_dashboard
from partner_detection.storage import DetectionStore
from tests.test_dashboard import Manager, SAMPLE, SETTINGS, SID, OTHER


def result():
    return {"provider": "modulate", "status": "complete", "label": "non-synthetic",
            "confidence": .91, "reason": "confident_non_synthetic", "streams": 1,
            "observations": 3, "accepted_frames": 300, "dropped_frames": 0,
            "submitted_audio_ms": 6000, "coverage_limited": True}


def client_for(store=None, manager=None):
    app = FastAPI()
    register_dashboard(app, SETTINGS, manager or Manager(), detection_store=store)
    return TestClient(app)


def test_polling_and_json_export_join_only_matching_results(tmp_path):
    store = DetectionStore(str(tmp_path))
    assert store.save(SID, result())
    with client_for(store) as client:
        response = client.get("/api/transcripts")
        snapshot = response.json()
        assert snapshot["detection"]["calls"] == [store.get(SID)]
        assert snapshot["sessions"][0]["detection"] == store.get(SID)
        assert "detection" not in snapshot["sessions"][1]
        assert snapshot["sessions"][0]["segments"] == SAMPLE["sessions"][0]["segments"]
        assert snapshot["sessions"][1]["segments"] == []
        assert "detection" not in SAMPLE["sessions"][0]
        exported = client.get(f"/api/transcripts/{SID}/export").json()
        assert exported["detection"] == store.get(SID)
        assert "detection" not in client.get(f"/api/transcripts/{OTHER}/export").json()
        assert response.headers["cache-control"] == "no-store"
        assert str(tmp_path) not in response.text
        assert "schema_version" not in snapshot["detection"]["calls"][0]


def test_results_remain_available_for_calls_without_transcription(tmp_path):
    class EmptyManager:
        def snapshot(self):
            return {"enabled": False, "sessions": []}
    store = DetectionStore(str(tmp_path))
    assert store.save(SID, result())
    with client_for(store, EmptyManager()) as client:
        snapshot = client.get("/api/transcripts").json()
        assert snapshot["sessions"] == []
        assert snapshot["detection"]["calls"][0]["call_sid"] == SID
        assert client.get(f"/api/transcripts/{SID}/export").status_code == 404


def test_absent_and_broken_detector_storage_do_not_break_transcripts():
    with client_for() as client:
        snapshot = client.get("/api/transcripts").json()
        assert snapshot["detection"] == {"enabled": False, "storage_error": "", "calls": []}
        assert all("detection" not in session for session in snapshot["sessions"])
    class BrokenStore:
        def snapshot(self):
            raise OSError("provider secret in a private path")
        def get(self, sid):
            raise OSError("provider secret in a private path")
    with client_for(BrokenStore()) as client:
        response = client.get("/api/transcripts")
        assert response.status_code == 200
        assert response.json()["detection"]["storage_error"] == "storage-unavailable"
        exported = client.get(f"/api/transcripts/{SID}/export")
        assert exported.status_code == 200 and "detection" not in exported.json()
        assert "provider secret" not in response.text + exported.text


def test_detector_result_has_no_public_write_or_provider_trigger(tmp_path):
    with client_for(DetectionStore(str(tmp_path))) as client:
        for method in ("post", "put", "patch", "delete"):
            assert client.request(method, "/api/transcripts", json=result()).status_code == 405
            assert client.request(method, f"/api/transcripts/{SID}/export", json=result()).status_code == 405
        assert client.post("/api/detection", json=result()).status_code == 404
        assert client.get("/api/detection/../secret").status_code == 404
