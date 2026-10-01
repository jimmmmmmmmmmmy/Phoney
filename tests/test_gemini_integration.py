"""Focused product and boundary checks; test helpers live in support."""

import asyncio

from fastapi.testclient import TestClient

from app import create_app
from call_details import CallDetailsStore
from transcription import storage

from support.gemini_integration import DOCUMENT, Provider, settings


def test_lifespan_worker_saves_gemini_summary_and_keeps_key_private(tmp_path):
    config = settings(tmp_path)
    provider = Provider()
    storage.save(config.transcript_storage_dir, DOCUMENT)
    app = create_app(config, summary_provider=provider)
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
