"""Application lifecycle and deployment isolation for recorded-call analysis."""

from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import create_app
from tests.test_media_webhooks import SETTINGS, Gateway, PARENT


class Worker:
    def __init__(self, settings, store, *, active_call_ids, can_run, provider):
        self.enabled = settings.modulate_backfill_enabled
        self.active_call_ids = active_call_ids
        self.can_run = can_run
        self.provider = provider
        self.active_count = 0
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    async def close(self):
        self.closed = True


def settings_for(tmp_path, **kwargs):
    return replace(SETTINGS, media_capture_enabled=True,
                   media_storage_dir=str(tmp_path / "audio"),
                   modulate_detection_enabled=True, modulate_backfill_enabled=True,
                   modulate_api_key="fixture-modulate-key",
                   detection_storage_dir=str(tmp_path / "detection"), **kwargs)


def test_recording_worker_lifecycle_and_drain_accounting(tmp_path, monkeypatch):
    monkeypatch.setattr("app.BackfillManager", Worker)
    provider = object()
    settings = settings_for(tmp_path)
    app = create_app(settings, gateway=Gateway(), detection_batch_provider=provider)
    worker = app.state.detection_backfill
    headers = {"Authorization": "Bearer " + settings.deploy_control_token}
    with TestClient(app) as client:
        assert worker.started and worker.provider is provider
        assert worker.can_run()
        assert client.get("/health").json()["detection_backfill_enabled"]
        worker.active_count = 1
        assert client.get("/internal/deploy", headers=headers).json()["pending_work"] == 1
        result = client.post("/internal/deploy", headers=headers, json={"draining": True})
        assert result.status_code == 200 and result.json()["pending_work"] == 1
        assert not worker.can_run()
        worker.active_count = 0
        assert client.get("/internal/deploy", headers=headers).json()["pending_work"] == 0
    assert worker.closed


def test_candidate_cannot_start_billable_recording_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr("app.BackfillManager", Worker)
    app = create_app(settings_for(tmp_path, deploy_commit="a" * 40,
                     deploy_trigger_path=str(tmp_path / "deploy.trigger")), gateway=Gateway())
    with TestClient(app):
        assert not app.state.detection_backfill.can_run()


def test_backfill_excludes_active_calls_and_finalizing_live_detector(tmp_path, monkeypatch):
    monkeypatch.setattr("app.BackfillManager", Worker)
    app = create_app(settings_for(tmp_path), gateway=Gateway())
    worker = app.state.detection_backfill
    app.state.switchboard.sessions[PARENT] = SimpleNamespace(phase="bridging")
    assert PARENT in worker.active_call_ids()
    app.state.switchboard.sessions[PARENT].phase = "ended"
    assert PARENT not in worker.active_call_ids()
    app.state.live_detection._pending_by_call[PARENT] = {object()}
    assert PARENT in worker.active_call_ids()
