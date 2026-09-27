"""Post-call background scheduling with fake providers; no external requests."""

import asyncio
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from call_details import CallDetailsStore, transcript_fingerprint
from gemini_summary import SummaryError
from summaries import SummaryManager
import summaries

CALL = "CA" + "1" * 32


def document():
    return {"call_sid": CALL, "stream_sid": "MZ" + "2" * 32, "status": "completed",
            "started_at": "2026-09-26T10:00:00+00:00", "ended_at": "2026-09-26T10:01:00+00:00",
            "segments": [{"id": "inbound-0", "track": "inbound", "start_ms": 0, "end_ms": 1000,
                          "text": "Fixture callback request.", "confidence": .9}]}


def settings(**changes):
    return SimpleNamespace(**({"gemini_api_key": "fixture-key", "transcription_enabled": True,
        "call_details_storage_dir": "/fixture/details", "gemini_summary_model": "gemini-3.8-flash"} | changes))


class Transcription:
    def __init__(self, documents=None): self.documents = documents if documents is not None else [document()]
    def snapshot(self): return {"sessions": deepcopy(self.documents)}


class ProviderError(Exception):
    def __init__(self, code, retryable=False):
        self.code, self.retryable = code, retryable


class Provider:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.brief_calls = []
        self.started = asyncio.Event()
        self.gate = None
        self.concurrent = self.maximum = self.closed = 0
    async def summarize(self, doc):
        self.calls.append(deepcopy(doc))
        self.concurrent += 1
        self.maximum = max(self.maximum, self.concurrent)
        self.started.set()
        try:
            if self.gate: await self.gate.wait()
            outcome = self.outcomes.pop(0) if self.outcomes else "Caller requested a callback."
            if isinstance(outcome, Exception): raise outcome
            return outcome
        finally:
            self.concurrent -= 1
    async def summarize_brief(self, doc):
        self.brief_calls.append(deepcopy(doc))
        return "Caller requested a callback."
    async def close(self): self.closed += 1


class Store:
    """Tiny durable fake for worker tests; real store has separate schema tests."""
    def __init__(self, path=None):
        self.path = path
        self.jobs = json.loads(path.read_text()) if path and path.exists() else {}
        self.saved = {}
        self.state_calls = 0
        self.begin_result = self.save_result = True
        self.after_begin = None
        self.save_started = threading.Event()
        self.save_gate = None
    def _persist(self):
        if self.path: self.path.write_text(json.dumps(self.jobs))
    def summary_state(self, sid, doc, *, kind="detailed"):
        key = sid if kind == "detailed" else sid + "/brief"
        self.state_calls += 1
        fingerprint = transcript_fingerprint(doc)
        if self.saved.get(key, {}).get("fingerprint") == fingerprint:
            return {"status": "completed", "attempts": 0, "retry_at": 0, "error": ""}
        job = self.jobs.get(key, {})
        if job.get("fingerprint") == fingerprint: return deepcopy(job)
        return {"status": "missing", "attempts": 0, "retry_at": 0, "error": ""}
    def begin_summary(self, sid, doc, *, kind="detailed"):
        key = sid if kind == "detailed" else sid + "/brief"
        if not self.begin_result: return False
        state = self.summary_state(sid, doc, kind=kind)
        if state["status"] == "completed" or state["attempts"] >= summaries.MAX_ATTEMPTS: return False
        self.jobs[key] = {"status": "pending", "attempts": state["attempts"] + 1, "retry_at": 0,
                          "error": "", "fingerprint": transcript_fingerprint(doc)}
        self._persist()
        if self.after_begin: self.after_begin()
        return True
    def fail_summary(self, sid, doc, error, retry_at=0, *, kind="detailed"):
        key = sid if kind == "detailed" else sid + "/brief"
        if self.jobs.get(key, {}).get("fingerprint") != transcript_fingerprint(doc): return False
        self.jobs[key].update(status="failed", error=error, retry_at=retry_at)
        self._persist()
        return True
    def set_summary(self, sid, text, doc, source="agent", model=None, *, kind="detailed"):
        key = sid if kind == "detailed" else sid + "/brief"
        self.save_started.set()
        if self.save_gate: self.save_gate.wait(timeout=2)
        if not self.save_result: return False
        self.saved[key] = {"text": text, "source": source, "model": model,
                           "fingerprint": transcript_fingerprint(doc)}
        return True


@pytest.mark.parametrize("change", [{"gemini_api_key": ""}, {"transcription_enabled": False},
                                    {"call_details_storage_dir": ""}])
def test_disabled_worker_never_starts_storage_or_provider(change):
    async def run():
        store, provider = Store(), Provider()
        manager = SummaryManager(settings(**change), Transcription(), store, provider=provider)
        manager.start()
        await manager.run_once()
        assert manager.active_count == 0 and manager._runner is None
        assert provider.calls == [] and store.state_calls == 0
        await manager.close()
    asyncio.run(run())


def test_finished_media_waits_until_phone_call_ends_then_saves_once():
    async def run():
        store, provider, active = Store(), Provider(), {CALL}
        manager = SummaryManager(settings(), Transcription(), store, lambda: active, provider)
        await manager.run_once()
        assert provider.calls == [] and store.state_calls == 0
        active.clear()
        await manager.run_once()
        assert store.saved[CALL]["source"] == "gemini"
        assert store.saved[CALL]["model"] == "gemini-3.8-flash"
        await manager.run_once()
        assert len(provider.calls) == 1 and manager.active_count == 0
        assert len(provider.brief_calls) == 1 and store.saved[CALL + "/brief"]["source"] == "gemini"
        await manager.run_once()
        assert len(provider.calls) == len(provider.brief_calls) == 1
        await manager.close()
    asyncio.run(run())


def test_only_ended_final_text_is_eligible_and_existing_agent_summary_is_preserved():
    async def run():
        for changed in ({"ended_at": None}, {"segments": []}, {"call_sid": "../invalid"}):
            store, provider = Store(), Provider()
            manager = SummaryManager(settings(), Transcription([document() | changed]), store, provider=provider)
            await manager.run_once()
            assert provider.calls == []
            await manager.close()
        store, provider = Store(), Provider()
        store.set_summary(CALL, "Operator summary.", document())
        manager = SummaryManager(settings(), Transcription(), store, provider=provider)
        await manager.run_once()
        assert provider.calls == [] and store.saved[CALL]["source"] == "agent"
        assert len(provider.brief_calls) == 1
        await manager.close()
    asyncio.run(run())


def test_concurrent_polls_do_not_duplicate_provider_requests():
    async def run():
        store, provider = Store(), Provider()
        provider.gate = asyncio.Event()
        manager = SummaryManager(settings(), Transcription(), store, provider=provider)
        first = asyncio.create_task(manager.run_once())
        await asyncio.wait_for(provider.started.wait(), 1)
        assert manager.active_count == 1
        await asyncio.gather(*(manager.run_once() for _ in range(8)))
        assert len(provider.calls) == provider.maximum == 1
        provider.gate.set()
        await first
        assert manager.active_count == 0
        await manager.close()
    asyncio.run(run())


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


@pytest.mark.parametrize("attempts,expected_calls", [(2, 1), (4, 1), (5, 0)])
def test_crashed_pending_attempt_is_retried_only_within_budget(attempts, expected_calls):
    async def run():
        store, provider = Store(), Provider()
        store.jobs[CALL] = {"status": "pending", "attempts": attempts, "retry_at": 0, "error": "",
                            "fingerprint": transcript_fingerprint(document())}
        manager = SummaryManager(settings(), Transcription(), store, provider=provider)
        await manager.run_once()
        assert len(provider.calls) == expected_calls
        if not expected_calls: assert store.jobs[CALL]["error"] == "retry-exhausted"
        await manager.close()
    asyncio.run(run())


def test_permanent_failure_does_not_retry_or_expose_untrusted_error_code():
    async def run():
        store, provider = Store(), Provider(ProviderError("private credential: value", False))
        manager = SummaryManager(settings(), Transcription(), store, provider=provider)
        await manager.run_once()
        await manager.run_once()
        assert len(provider.calls) == 1 and store.jobs[CALL]["error"] == "provider-error"
        assert store.jobs[CALL]["retry_at"] == 0
        await manager.close()
    asyncio.run(run())


def test_private_trace_retains_transient_failure_cause_after_success(caplog, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(summaries.time, "time", lambda: clock[0])
    caplog.set_level("INFO", logger="uvicorn.error")

    async def run():
        store = Store()
        provider = Provider(SummaryError("rate_limited", True, http_status=429),
                            "PRIVATE GENERATED SUMMARY")
        manager = SummaryManager(settings(), Transcription(), store, provider=provider)
        await manager.run_once()
        assert store.jobs[CALL]["retry_at"] == 1030
        clock[0] = 1030
        await manager.run_once()
        assert store.saved[CALL]["text"] == "PRIVATE GENERATED SUMMARY"
        await manager.close()

    asyncio.run(run())
    records = [json.loads(record.message.removeprefix("summary_trace ")) for record in caplog.records
               if record.message.startswith("summary_trace ")]
    assert [record["event"] for record in records] == ["started", "failed", "started", "completed"]
    failed = records[1]
    assert failed["error"] == "rate_limited" and failed["http_status"] == 429
    assert failed["attempt"] == 1 and failed["retry_at"] == 1030
    assert records[-1]["attempt"] == 2
    for record in records:
        assert record["call_sid"] == CALL and record["kind"] == "detailed"
        assert record["model"] == "gemini-3.8-flash"
    assert type(failed["elapsed_ms"]) is int and failed["elapsed_ms"] >= 0
    for private in ("fixture-key", "Fixture callback request.", "PRIVATE GENERATED SUMMARY"):
        assert private not in caplog.text


def test_trace_rejects_untrusted_provider_error_metadata(caplog):
    caplog.set_level("INFO", logger="uvicorn.error")

    async def run():
        error = ProviderError("privatecredential", True)
        error.http_status = "PRIVATE HTTP RESPONSE"
        store = Store()
        manager = SummaryManager(settings(), Transcription(), store, provider=Provider(error))
        await manager.run_once()
        assert store.jobs[CALL]["error"] == "provider-error"
        await manager.close()

    asyncio.run(run())
    assert "privatecredential" not in caplog.text and "PRIVATE HTTP RESPONSE" not in caplog.text
    failure = next(json.loads(record.message.removeprefix("summary_trace ")) for record in caplog.records
                   if record.message.startswith("summary_trace ") and '"event":"failed"' in record.message)
    assert failure["error"] == "provider-error" and "http_status" not in failure


@pytest.mark.parametrize("change", ["transcript", "call-active", "operator-summary"])
def test_result_is_rechecked_after_provider_and_never_overwrites_newer_evidence(change):
    async def run():
        store, provider, transcript, active = Store(), Provider(), Transcription(), set()
        provider.gate = asyncio.Event()
        manager = SummaryManager(settings(), transcript, store, lambda: active, provider)
        running = asyncio.create_task(manager.run_once())
        await asyncio.wait_for(provider.started.wait(), 1)
        if change == "transcript": transcript.documents[0]["segments"][0]["text"] = "Corrected speech."
        elif change == "call-active": active.add(CALL)
        else: store.set_summary(CALL, "Human-authored result.", document())
        provider.gate.set()
        await running
        if change == "operator-summary":
            assert store.saved[CALL]["source"] == "agent"
        else:
            assert store.saved == {} and store.jobs[CALL]["error"] == "transcript-changed"
        if change == "transcript":
            await manager.run_once()
            assert len(provider.calls) == 2 and store.saved[CALL]["fingerprint"] == transcript_fingerprint(transcript.documents[0])
        await manager.close()
    asyncio.run(run())


def test_managed_process_gate_blocks_candidate_before_any_storage_and_before_network():
    async def run():
        allowed = [False]
        store, provider = Store(), Provider()
        manager = SummaryManager(settings(), Transcription(), store, provider=provider, can_run=lambda: allowed[0])
        await manager.run_once()
        assert store.state_calls == 0 and provider.calls == []
        allowed[0] = True
        store.after_begin = lambda: allowed.__setitem__(0, False)
        await manager.run_once()
        assert provider.calls == [] and store.jobs[CALL]["error"] == "interrupted"
        await manager.close()
    asyncio.run(run())


def test_already_running_generation_finishes_when_deployment_starts_draining():
    async def run():
        allowed, store, provider = [True], Store(), Provider()
        provider.gate = asyncio.Event()
        manager = SummaryManager(settings(), Transcription(), store, provider=provider, can_run=lambda: allowed[0])
        running = asyncio.create_task(manager.run_once())
        await asyncio.wait_for(provider.started.wait(), 1)
        allowed[0] = False
        provider.gate.set()
        await running
        assert store.saved[CALL]["source"] == "gemini"
        await manager.close()
    asyncio.run(run())


def test_storage_failure_never_starts_unaccounted_generation_or_retries_a_saved_failure():
    async def run():
        store, provider = Store(), Provider()
        store.begin_result = False
        manager = SummaryManager(settings(), Transcription(), store, provider=provider)
        await manager.run_once()
        assert provider.calls == []
        store.begin_result, store.save_result = True, False
        await manager.run_once()
        await manager.run_once()
        assert len(provider.calls) == 1 and store.jobs[CALL]["error"] == "storage-error"
        assert manager.active_count == 0
        await manager.close()
    asyncio.run(run())


def test_active_count_includes_final_disk_write():
    async def run():
        store, provider = Store(), Provider()
        store.save_gate = threading.Event()
        manager = SummaryManager(settings(), Transcription(), store, provider=provider)
        running = asyncio.create_task(manager.run_once())
        assert await asyncio.to_thread(store.save_started.wait, 1)
        assert provider.concurrent == 0 and manager.active_count == 1
        store.save_gate.set()
        await running
        assert manager.active_count == 0
        await manager.close()
    asyncio.run(run())


def test_provider_deadline_is_retryable_and_shutdown_closes_without_orphans(monkeypatch):
    monkeypatch.setattr(summaries, "REQUEST_SECONDS", .01)
    async def run():
        store, provider = Store(), Provider()
        provider.gate = asyncio.Event()
        manager = SummaryManager(settings(), Transcription(), store, provider=provider)
        await manager.run_once()
        assert store.jobs[CALL]["error"] == "provider-timeout" and store.jobs[CALL]["retry_at"] > 0
        await manager.close()
        assert provider.closed == 1 and provider.concurrent == manager.active_count == 0
        await manager.close()
        assert provider.closed == 1
    asyncio.run(run())


def test_shutdown_records_interrupted_attempt_and_stops_runner():
    async def run():
        store, provider = Store(), Provider()
        provider.gate = asyncio.Event()
        manager = SummaryManager(settings(), Transcription(), store, provider=provider)
        manager.start()
        runner = manager._runner
        manager.start()
        assert manager._runner is runner
        await asyncio.wait_for(provider.started.wait(), 1)
        await manager.close()
        assert runner.done() and manager.active_count == 0 and provider.closed == 1
        assert store.jobs[CALL]["error"] == "interrupted" and store.jobs[CALL]["retry_at"] > 0
        await manager.run_once()
        assert len(provider.calls) == 1
    asyncio.run(run())


def test_real_details_store_retry_and_generated_summary_survive_restart(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(summaries.time, "time", lambda: clock[0])
    async def run():
        config = settings(call_details_storage_dir=str(tmp_path))
        store = CallDetailsStore(str(tmp_path))
        first = SummaryManager(config, Transcription(), store,
                               provider=Provider(ProviderError("rate_limited", True)))
        await first.run_once()
        assert store.summary_state(CALL, document()) == {
            "status": "failed", "attempts": 1, "retry_at": 1030, "error": "rate_limited"}
        await first.close()
        clock[0] = 1030
        restarted = CallDetailsStore(str(tmp_path))
        second = SummaryManager(config, Transcription(), restarted, provider=Provider())
        await second.run_once()
        record = CallDetailsStore(str(tmp_path)).snapshot([document()])["calls"][0]
        assert record["summary"]["source"] == "gemini"
        assert record["summary"]["model"] == config.gemini_summary_model
        await second.run_once()
        assert len(second.provider.calls) == 1
        await second.close()
    asyncio.run(run())


def test_provider_recovers_after_three_failures_without_losing_the_transcript(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(summaries.time, "time", lambda: clock[0])

    async def run():
        config = settings(call_details_storage_dir=str(tmp_path))
        provider = Provider(*(ProviderError("provider_unavailable", True) for _ in range(3)),
                            "Caller requested a callback; New College DS confirmed.")
        for instant in (1000, 1030, 1150):
            clock[0] = instant
            manager = SummaryManager(config, Transcription(), CallDetailsStore(str(tmp_path)),
                                     provider=provider)
            await manager.run_once()
            await manager.close()
        store = CallDetailsStore(str(tmp_path))
        record = store.snapshot([document()])["calls"][0]
        assert record["summary"] is None
        assert record["summary_status"] == "retrying" and record["summary_retry_at"] == 1750
        assert not {"error", "attempts", "fingerprint"} & record.keys()
        clock[0] = 1749
        manager = SummaryManager(config, Transcription(), store, provider=provider)
        await manager.run_once()
        assert len(provider.calls) == 3
        clock[0] = 1750
        await manager.run_once()
        await manager.run_once()
        record = CallDetailsStore(str(tmp_path)).snapshot([document()])["calls"][0]
        assert record["summary_status"] == "completed"
        assert record["summary"]["text"].startswith("Caller requested a callback")
        assert "summary_retry_at" not in record and len(provider.calls) == 4
        await manager.close()

    asyncio.run(run())


def test_old_exhausted_transient_failure_recovers_without_resetting_attempt_budget(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(summaries.time, "time", lambda: clock[0])
    store = CallDetailsStore(str(tmp_path))
    for attempt in range(3):
        assert store.begin_summary(CALL, document())
        assert store.fail_summary(CALL, document(), "provider_unavailable", retry_at=1 if attempt < 2 else 0)

    async def run():
        provider = Provider()
        manager = SummaryManager(settings(), Transcription(), store, provider=provider)
        await manager.run_once()
        state = store.summary_state(CALL, document())
        assert state["attempts"] == 3 and state["retry_at"] == 1600
        await manager.run_once()
        assert provider.calls == []
        await manager.close()
        clock[0] = 1600
        restarted = CallDetailsStore(str(tmp_path))
        resumed = SummaryManager(settings(), Transcription(), restarted, provider=provider)
        await resumed.run_once()
        assert len(provider.calls) == 1
        assert restarted.snapshot([document()])["calls"][0]["summary_status"] == "completed"
        assert restarted._records[CALL]["summary_job"]["attempts"] == 4
        await resumed.close()

    asyncio.run(run())


@pytest.mark.parametrize("error", ["billing_required", "authentication_failed", "blocked", "invalid_response"])
def test_longer_retry_policy_does_not_resume_permanent_failures(tmp_path, error):
    store = CallDetailsStore(str(tmp_path))
    assert store.begin_summary(CALL, document())
    assert store.fail_summary(CALL, document(), error)

    async def run():
        provider = Provider()
        manager = SummaryManager(settings(), Transcription(), store, provider=provider)
        await manager.run_once()
        await manager.run_once()
        assert provider.calls == []
        record = store.snapshot([document()])["calls"][0]
        assert record["summary_status"] == "failed" and "summary_retry_at" not in record
        await manager.close()

    asyncio.run(run())
