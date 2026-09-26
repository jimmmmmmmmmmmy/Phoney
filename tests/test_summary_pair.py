"""Detailed and brief summaries have independent durable jobs; no live requests."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from call_details import CallDetailsStore, transcript_fingerprint
from summaries import SummaryManager
import summaries


CALL = "CA" + "a" * 32
DETAILED = "Caller requested an afternoon callback. New College DS agreed to call tomorrow at 2 PM."
BRIEF = "Callback requested for tomorrow at 2 PM."


def document():
    return {
        "call_sid": CALL, "stream_sid": "MZ" + "b" * 32,
        "status": "completed", "started_at": "2026-09-26T10:00:00+00:00",
        "ended_at": "2026-09-26T10:01:00+00:00",
        "segments": [
            {"id": "inbound-0", "track": "inbound", "start_ms": 0,
             "end_ms": 1000, "text": "Please call tomorrow at 2 PM."},
            {"id": "outbound-0", "track": "outbound", "start_ms": 1200,
             "end_ms": 2200, "text": "We will call tomorrow at 2 PM."},
        ],
    }


class Transcription:
    def __init__(self):
        self.documents = [document()]

    def snapshot(self):
        return {"sessions": deepcopy(self.documents)}


class ProviderError(Exception):
    def __init__(self, code, retryable=False):
        self.code, self.retryable = code, retryable


class DualProvider:
    def __init__(self, *, detailed=(), brief=()):
        self.outcomes = {"detailed": list(detailed), "brief": list(brief)}
        self.calls = []
        self.started = {kind: asyncio.Event() for kind in self.outcomes}
        self.gates = {}
        self.concurrent = self.maximum = self.closed = 0

    async def _generate(self, kind, doc):
        self.calls.append((kind, deepcopy(doc)))
        self.concurrent += 1
        self.maximum = max(self.maximum, self.concurrent)
        self.started[kind].set()
        try:
            if kind in self.gates:
                await self.gates[kind].wait()
            outcome = (self.outcomes[kind].pop(0) if self.outcomes[kind]
                       else DETAILED if kind == "detailed" else BRIEF)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.concurrent -= 1

    async def summarize(self, doc):
        return await self._generate("detailed", doc)

    async def summarize_brief(self, doc):
        return await self._generate("brief", doc)

    async def close(self):
        self.closed += 1


def manager_for(tmp_path, store, provider, transcription=None, **kwargs):
    settings = SimpleNamespace(
        gemini_api_key="fixture-only", transcription_enabled=True,
        call_details_storage_dir=str(tmp_path), gemini_summary_model="gemini-3.8-flash",
    )
    return SummaryManager(settings, transcription or Transcription(), store,
                          provider=provider, **kwargs)


def disk_record(tmp_path):
    return json.loads((tmp_path / (CALL + ".json")).read_text())


def public_record(store, doc=None):
    return store.snapshot([doc or document()])["calls"][0]


def test_two_polls_create_two_distinct_outputs_and_restart_never_duplicates(tmp_path):
    async def run():
        store, provider = CallDetailsStore(str(tmp_path)), DualProvider()
        manager = manager_for(tmp_path, store, provider)
        try:
            await manager.run_once()
            assert [kind for kind, _ in provider.calls] == ["detailed"]
            first = public_record(store)
            assert first["summary"]["text"] == DETAILED
            assert first["brief_summary"] is None
            await manager.run_once()
            assert [kind for kind, _ in provider.calls] == ["detailed", "brief"]
            pair = public_record(CallDetailsStore(str(tmp_path)))
            for key, expected in (("summary", DETAILED), ("brief_summary", BRIEF)):
                assert pair[key]["text"] == expected
                assert pair[key]["source"] == "gemini"
                assert pair[key]["model"] == "gemini-3.8-flash"
                assert pair[key]["created_at"]
                assert pair[key + "_status"] == "completed"
                assert "fingerprint" not in pair[key]
            assert disk_record(tmp_path)["summary_job"]["attempts"] == 1
            assert disk_record(tmp_path)["brief_summary_job"]["attempts"] == 1
        finally:
            await manager.close()
        restarted_provider = DualProvider()
        restarted = manager_for(tmp_path, CallDetailsStore(str(tmp_path)), restarted_provider)
        try:
            for _ in range(4):
                await restarted.run_once()
            assert restarted_provider.calls == []
        finally:
            await restarted.close()

    asyncio.run(run())


def test_legacy_authored_detailed_summary_is_retained_while_only_brief_catches_up(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    assert store.set_summary(CALL, "Operator-authored detailed summary.", document())
    record = disk_record(tmp_path)
    authored = deepcopy(record["summary"])
    for key in ("brief_summary", "brief_summary_job", "summary_job"):
        record.pop(key, None)
    (tmp_path / (CALL + ".json")).write_text(json.dumps(record))

    async def run():
        reloaded, provider = CallDetailsStore(str(tmp_path)), DualProvider()
        assert reloaded.storage_error == ""
        manager = manager_for(tmp_path, reloaded, provider)
        try:
            await manager.run_once()
            await manager.run_once()
            assert [kind for kind, _ in provider.calls] == ["brief"]
            saved = disk_record(tmp_path)
            assert saved["summary"] == authored
            assert saved["brief_summary"]["text"] == BRIEF
            assert public_record(reloaded)["summary"]["source"] == "agent"
        finally:
            await manager.close()

    asyncio.run(run())


@pytest.mark.parametrize("failed_kind", ["detailed", "brief"])
@pytest.mark.parametrize("retryable", [False, True])
def test_one_failure_never_blocks_or_regenerates_the_completed_counterpart(
        tmp_path, monkeypatch, failed_kind, retryable):
    clock = [1000.0]
    monkeypatch.setattr(summaries.time, "time", lambda: clock[0])
    error = "provider_unavailable" if retryable else "blocked"

    async def run():
        provider = DualProvider(**{failed_kind: [ProviderError(error, retryable)]})
        store = CallDetailsStore(str(tmp_path))
        manager = manager_for(tmp_path, store, provider)
        completed_kind = "brief" if failed_kind == "detailed" else "detailed"
        key = "summary" if failed_kind == "detailed" else "brief_summary"
        completed_key = "summary" if completed_kind == "detailed" else "brief_summary"
        try:
            await manager.run_once()
            await manager.run_once()
            assert [kind for kind, _ in provider.calls] == ["detailed", "brief"]
            saved_counterpart = deepcopy(disk_record(tmp_path)[completed_key])
            visible = public_record(store)
            assert visible[key] is None
            assert visible[key + "_status"] == ("retrying" if retryable else "failed")
            assert visible[completed_key + "_status"] == "completed"
            if retryable:
                assert visible[key + "_retry_at"] == 1030
            else:
                assert key + "_retry_at" not in visible
            await manager.run_once()
            assert len(provider.calls) == 2
        finally:
            await manager.close()

        clock[0] = 1030
        resumed_provider = DualProvider()
        resumed = manager_for(tmp_path, CallDetailsStore(str(tmp_path)), resumed_provider)
        try:
            await resumed.run_once()
            await resumed.run_once()
            assert [kind for kind, _ in resumed_provider.calls] == ([failed_kind] if retryable else [])
            assert disk_record(tmp_path)[completed_key] == saved_counterpart
            assert disk_record(tmp_path)[key + "_job"]["attempts"] == (2 if retryable else 1)
        finally:
            await resumed.close()

    asyncio.run(run())


def test_each_job_has_its_own_five_attempt_budget_across_restarts(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(summaries.time, "time", lambda: clock[0])

    async def run():
        all_calls = []
        for attempt, instant in enumerate((1000, 1030, 1150, 1750, 3550), start=1):
            clock[0] = instant
            provider = DualProvider(detailed=[ProviderError("provider_unavailable", True)],
                                    brief=[ProviderError("rate_limited", True)])
            manager = manager_for(tmp_path, CallDetailsStore(str(tmp_path)), provider)
            try:
                await manager.run_once()
                await manager.run_once()
                await manager.run_once()
                assert [kind for kind, _ in provider.calls] == ["detailed", "brief"]
                all_calls.extend(provider.calls)
                saved = disk_record(tmp_path)
                for key in ("summary_job", "brief_summary_job"):
                    assert saved[key]["attempts"] == attempt
                    assert saved[key]["retry_at"] == (instant + summaries.RETRY_DELAYS[attempt - 1]
                                                       if attempt < 5 else 0)
            finally:
                await manager.close()
        clock[0] = 100_000
        last_provider = DualProvider()
        last = manager_for(tmp_path, CallDetailsStore(str(tmp_path)), last_provider)
        try:
            await last.run_once()
            assert last_provider.calls == []
            assert len(all_calls) == 10
        finally:
            await last.close()

    asyncio.run(run())


def test_stale_inflight_brief_result_is_rejected_and_both_new_fingerprints_catch_up(tmp_path):
    async def run():
        store, provider, transcription = CallDetailsStore(str(tmp_path)), DualProvider(), Transcription()
        assert store.set_summary(CALL, "Old detailed result.", document())
        provider.gates["brief"] = asyncio.Event()
        manager = manager_for(tmp_path, store, provider, transcription)
        try:
            running = asyncio.create_task(manager.run_once())
            await asyncio.wait_for(provider.started["brief"].wait(), 1)
            transcription.documents[0]["segments"][0]["text"] = "Please call next Monday instead."
            changed = transcription.documents[0]
            provider.gates["brief"].set()
            await running
            visible = public_record(store, changed)
            assert visible["summary"] is visible["brief_summary"] is None
            saved = disk_record(tmp_path)
            assert saved["brief_summary"] is None
            assert saved["brief_summary_job"]["error"] == "transcript-changed"
            await manager.run_once()
            await manager.run_once()
            saved = disk_record(tmp_path)
            for key in ("summary", "brief_summary"):
                assert saved[key]["fingerprint"] == transcript_fingerprint(changed)
                assert saved[key + "_job"]["attempts"] == 1
            assert [kind for kind, _ in provider.calls] == ["brief", "detailed", "brief"]
        finally:
            await manager.close()

    asyncio.run(run())


def test_store_enforces_brief_limit_without_restricting_or_overwriting_detailed(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    assert store.set_summary(CALL, "D" * 1000, document(), kind="detailed")
    assert store.set_summary(CALL, "B" * 280, document(), kind="brief")
    before = disk_record(tmp_path)
    assert not store.set_summary(CALL, "B" * 281, document(), kind="brief")
    assert disk_record(tmp_path) == before
    reloaded = public_record(CallDetailsStore(str(tmp_path)))
    assert len(reloaded["summary"]["text"]) == 1000
    assert len(reloaded["brief_summary"]["text"]) == 280


def test_worker_rejects_oversized_brief_without_truncating_or_disturbing_detailed(tmp_path):
    async def run():
        store = CallDetailsStore(str(tmp_path))
        provider = DualProvider(detailed=["D" * 1000], brief=["B" * 281])
        manager = manager_for(tmp_path, store, provider)
        try:
            await manager.run_once()
            await manager.run_once()
            await manager.run_once()
            visible = public_record(store)
            assert visible["summary"]["text"] == "D" * 1000
            assert visible["brief_summary"] is None
            assert visible["brief_summary_status"] == "failed"
            assert [kind for kind, _ in provider.calls] == ["detailed", "brief"]
        finally:
            await manager.close()

    asyncio.run(run())


def test_concurrent_polls_remain_serial_for_both_requests(tmp_path):
    async def run():
        store, provider = CallDetailsStore(str(tmp_path)), DualProvider()
        provider.gates = {kind: asyncio.Event() for kind in ("detailed", "brief")}
        manager = manager_for(tmp_path, store, provider)
        try:
            for count, kind in enumerate(("detailed", "brief"), start=1):
                running = asyncio.create_task(manager.run_once())
                await asyncio.wait_for(provider.started[kind].wait(), 1)
                assert manager.active_count == 1
                await asyncio.gather(*(manager.run_once() for _ in range(8)))
                assert len(provider.calls) == count
                assert provider.maximum == 1
                provider.gates[kind].set()
                await running
                assert manager.active_count == 0
            await asyncio.gather(*(manager.run_once() for _ in range(8)))
            assert len(provider.calls) == 2
        finally:
            await manager.close()
        assert provider.closed == 1 and provider.concurrent == 0

    asyncio.run(run())


def test_deployment_gate_allows_inflight_result_but_defers_second_job(tmp_path):
    async def run():
        store, provider, allowed = CallDetailsStore(str(tmp_path)), DualProvider(), [False]
        provider.gates["detailed"] = asyncio.Event()
        manager = manager_for(tmp_path, store, provider, can_run=lambda: allowed[0])
        try:
            await manager.run_once()
            assert provider.calls == []
            allowed[0] = True
            running = asyncio.create_task(manager.run_once())
            await asyncio.wait_for(provider.started["detailed"].wait(), 1)
            allowed[0] = False
            provider.gates["detailed"].set()
            await running
            await manager.run_once()
            assert [kind for kind, _ in provider.calls] == ["detailed"]
            assert public_record(store)["summary"]["text"] == DETAILED
            assert public_record(store)["brief_summary"] is None
            allowed[0] = True
            await manager.run_once()
            assert [kind for kind, _ in provider.calls] == ["detailed", "brief"]
        finally:
            await manager.close()

    asyncio.run(run())


def test_authored_brief_written_during_generation_is_preserved_independently(tmp_path):
    async def run():
        store, provider = CallDetailsStore(str(tmp_path)), DualProvider()
        assert store.set_summary(CALL, "Authored detailed result.", document())
        detailed = deepcopy(disk_record(tmp_path)["summary"])
        provider.gates["brief"] = asyncio.Event()
        manager = manager_for(tmp_path, store, provider)
        try:
            running = asyncio.create_task(manager.run_once())
            await asyncio.wait_for(provider.started["brief"].wait(), 1)
            assert store.set_summary(CALL, "Operator edited brief.", document(), kind="brief")
            provider.gates["brief"].set()
            await running
            await manager.run_once()
            saved = disk_record(tmp_path)
            assert saved["summary"] == detailed
            assert saved["brief_summary"]["text"] == "Operator edited brief."
            assert saved["brief_summary"]["source"] == "agent"
            assert [kind for kind, _ in provider.calls] == ["brief"]
        finally:
            await manager.close()

    asyncio.run(run())
