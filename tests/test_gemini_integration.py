"""Automatic summaries persist and deploy candidates never call a provider."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os

from fastapi.testclient import TestClient

from app import create_app
from call_details import CallDetailsStore, MAX_SUMMARY_ATTEMPTS
from config import Settings

SID = "CA" + "a" * 32
DOCUMENT = {"call_sid": SID, "started_at": "2026-09-26T12:00:00+00:00",
            "ended_at": "2026-09-26T12:00:20+00:00", "status": "completed", "tracks": {},
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


def test_lifespan_worker_saves_gemini_summary_and_keeps_key_private(tmp_path):
    config = settings(tmp_path)
    provider = Provider()
    app = create_app(config, summary_provider=provider)
    app.state.transcription.history = [deepcopy(DOCUMENT)]
    with TestClient(app) as client:
        client.portal.call(app.state.summaries.run_once)
        # A lifespan-started request may still hold the serial worker lock.
        async def settled():
            for _ in range(100):
                if app.state.call_details.snapshot([DOCUMENT])["calls"][0]["summary"]:
                    return
                await asyncio.sleep(.01)
        client.portal.call(settled)
        result = client.get("/api/transcripts")
        record = result.json()["call_details"]["calls"][0]
        assert record["summary"]["source"] == "gemini"
        assert record["summary"]["model"] == config.gemini_summary_model
        assert record["summary_status"] == "completed"
        assert config.gemini_api_key not in result.text
        assert client.get("/health").json()["summaries_enabled"] is True
        client.portal.call(app.state.summaries.run_once)
        assert provider.calls == provider.brief_calls == 1
        paired = client.get("/api/transcripts").json()["call_details"]["calls"][0]
        assert paired["brief_summary_status"] == "completed"
        assert paired["brief_summary"]["source"] == "gemini"
        client.portal.call(app.state.summaries.run_once)
        assert provider.calls == provider.brief_calls == 1
    assert provider.closed
    assert CallDetailsStore(config.call_details_storage_dir).snapshot([DOCUMENT])["calls"][0]["summary"] == record["summary"]
    assert CallDetailsStore(config.call_details_storage_dir).snapshot([DOCUMENT])["calls"][0]["brief_summary"] == paired["brief_summary"]


def test_only_supervisor_owned_app_can_summarize_and_drain_stops_new_jobs(tmp_path):
    config = replace(settings(tmp_path), deploy_commit="b" * 40,
                     deploy_trigger_path=str(tmp_path / "deploy.trigger"))
    provider = Provider()
    app = create_app(config, summary_provider=provider)
    app.state.transcription.history = [deepcopy(DOCUMENT)]
    state_file = tmp_path / "dev.json"

    async def run():
        await app.state.summaries.run_once()
        assert provider.calls == 0  # Missing ownership record.
        state_file.write_text(json.dumps({"app": {"pid": os.getpid() + 1, "commit": config.deploy_commit}}))
        await app.state.summaries.run_once()
        assert provider.calls == 0  # Candidate process.
        state_file.write_text(json.dumps({"app": {"pid": os.getpid(), "commit": config.deploy_commit}}))
        app.state.switchboard.draining = True
        await app.state.summaries.run_once()
        assert provider.calls == 0
        app.state.switchboard.draining = False
        await app.state.summaries.run_once()
        assert provider.calls == 1
        await app.state.summaries.close()

    asyncio.run(run())


def test_summary_job_attempts_and_owner_retry_survive_restart(tmp_path):
    path = str(tmp_path / "details")
    store = CallDetailsStore(path)
    for attempt in range(1, MAX_SUMMARY_ATTEMPTS + 1):
        assert store.begin_summary(SID, DOCUMENT)
        assert store.summary_state(SID, DOCUMENT)["attempts"] == attempt
        assert store.fail_summary(SID, DOCUMENT, "rate_limited", retry_at=1)
        store = CallDetailsStore(path)
    assert not store.begin_summary(SID, DOCUMENT)
    assert store.retry_summary(SID, DOCUMENT)
    assert store.begin_summary(SID, DOCUMENT)
    assert store.summary_state(SID, DOCUMENT)["attempts"] == 1
    assert store.fail_summary(SID, DOCUMENT, "billing_required")
    assert not store.begin_summary(SID, DOCUMENT)
    assert store.snapshot([DOCUMENT])["calls"][0]["summary_status"] == "failed"


def test_transcript_change_resets_job_and_agent_edit_wins_provider_race(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    assert store.begin_summary(SID, DOCUMENT)
    assert store.fail_summary(SID, DOCUMENT, "billing_required")
    changed = deepcopy(DOCUMENT)
    changed["segments"][0]["text"] = "Please call next week."
    assert store.summary_state(SID, changed)["status"] == "missing"
    assert store.begin_summary(SID, changed)
    assert store.set_summary(SID, "Caller requested a call next week.", changed)
    assert store.set_summary(SID, "A generated summary.", changed, source="gemini", model="gemini-3.8-flash")
    record = store.snapshot([changed])["calls"][0]
    assert record["summary"]["source"] == "agent"
    assert record["summary"]["text"] == "Caller requested a call next week."


def test_legacy_authored_summaries_remain_readable(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    assert store.set_summary(SID, "Caller requested a callback tomorrow.", DOCUMENT)
    path = tmp_path / (SID + ".json")
    old = json.loads(path.read_text())
    old.pop("summary_job")
    old.pop("brief_summary")
    old.pop("brief_summary_job")
    path.write_text(json.dumps(old))
    restored = CallDetailsStore(str(tmp_path))
    assert restored.summary_state(SID, DOCUMENT)["status"] == "completed"
    assert restored.snapshot([DOCUMENT])["calls"][0]["summary"]["source"] == "agent"
