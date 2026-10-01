"""Focused product and boundary checks; test helpers live in support."""

import asyncio

from summaries import SummaryManager
import summaries

from support.summaries import CALL, Provider, ProviderError, Store, Transcription, settings


def test_retry_budget_and_backoff_survive_worker_and_store_restart(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(summaries.time, "time", lambda: clock[0])
    async def run():
        path = tmp_path / "jobs.json"
        provider = Provider(*(ProviderError("rate_limited", True) for _ in range(5)))
        for attempt, instant in enumerate((1000, 1030, 1150, 1750, 3550), start=1):
            clock[0] = instant
            store = Store(path)
            manager = SummaryManager(settings(), Transcription(), store, provider=provider)
            await manager.run_once()
            job = store.jobs[CALL]
            assert job["attempts"] == attempt and job["error"] == "rate_limited"
            assert job["retry_at"] == ({1: 1030, 2: 1150, 3: 1750, 4: 3550, 5: 0}[attempt])
            await manager.run_once()
            assert len(provider.calls) == attempt
            await manager.close()
        clock[0] = 100_000
        final = SummaryManager(settings(), Transcription(), Store(path), provider=provider)
        await final.run_once()
        assert len(provider.calls) == 5
        await final.close()
    asyncio.run(run())
