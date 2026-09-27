"""Old transcripts remain eligible without retaining the archive in dashboard RAM."""

import asyncio
from copy import deepcopy
import json
import os
from types import SimpleNamespace

import pytest

from call_details import CallDetailsStore, transcript_fingerprint
from scripts import call_details as commands
from summaries import MAX_SESSIONS, SummaryManager
from transcription import storage
from test_summary_pair import BRIEF, DETAILED, DualProvider, Transcription, document


def archived_document(number):
    return document() | {"call_sid": "CA" + f"{number:032x}"}


def saved_documents(tmp_path, numbers):
    path = str(tmp_path / "transcripts")
    documents = [archived_document(number) for number in numbers]
    for index, item in enumerate(documents):
        storage.save(path, item)
        os.utime(tmp_path / "transcripts" / (item["call_sid"] + ".json"), (1000 + index, 1000 + index))
    return documents


def completed_pair(store, item):
    for kind, text in (("detailed", DETAILED), ("brief", BRIEF)):
        assert store.set_summary(item["call_sid"], text, item, kind=kind)


def setup_manager(tmp_path, recent, store, provider, **kwargs):
    config = SimpleNamespace(gemini_api_key="fixture-only", transcription_enabled=True,
                             transcript_storage_dir=str(tmp_path / "transcripts"),
                             call_details_storage_dir=str(tmp_path / "details"),
                             gemini_summary_model="gemini-3.8-flash")
    transcription = Transcription()
    transcription.documents = deepcopy(recent)
    manager = SummaryManager(config, transcription, store, provider=provider, **kwargs)
    return manager, transcription


def test_old_retry_survives_recent_history_eviction_and_restart(tmp_path):
    old, *recent = saved_documents(tmp_path, range(11))
    assert old["call_sid"] not in {item["call_sid"] for item in storage.load(str(tmp_path / "transcripts"))}
    store = CallDetailsStore(str(tmp_path / "details"))
    for item in recent:
        completed_pair(store, item)
    assert store.set_summary(old["call_sid"], "Preserve the completed brief.", old, kind="brief")
    assert store.begin_summary(old["call_sid"], old)
    assert store.fail_summary(old["call_sid"], old, "provider_unavailable", retry_at=1)

    async def run():
        provider = DualProvider()
        manager, transcription = setup_manager(tmp_path, recent, store, provider)
        try:
            await manager.run_once()
            assert [(kind, item["call_sid"]) for kind, item in provider.calls] == [("detailed", old["call_sid"])]
            assert transcription.documents == recent
            assert store.summary_state(old["call_sid"], old)["status"] == "completed"
            record = json.loads((tmp_path / "details" / (old["call_sid"] + ".json")).read_text())
            assert record["summary_job"]["attempts"] == 2
            assert record["brief_summary"]["text"] == "Preserve the completed brief."
        finally:
            await manager.close()
        provider = DualProvider()
        manager, _ = setup_manager(tmp_path, recent, CallDetailsStore(str(tmp_path / "details")), provider)
        try:
            await manager.run_once()
            assert provider.calls == []
        finally:
            await manager.close()

    asyncio.run(run())


def test_archive_scans_one_bounded_page_and_eventually_reaches_later_jobs(tmp_path, monkeypatch):
    archived = saved_documents(tmp_path, range(MAX_SESSIONS + 1))
    store = CallDetailsStore(str(tmp_path / "details"))
    for item in archived[:-1]:
        completed_pair(store, item)
    reads = []
    load_call = storage.load_call

    def read(path, sid):
        reads.append(sid)
        return load_call(path, sid)

    monkeypatch.setattr(storage, "load_call", read)

    async def run():
        provider = DualProvider()
        manager, transcription = setup_manager(tmp_path, [], store, provider)
        try:
            await manager.run_once()
            assert len(reads) == MAX_SESSIONS
            assert provider.calls == []
            await manager.run_once()
            assert [(kind, item["call_sid"]) for kind, item in provider.calls] == [("detailed", archived[-1]["call_sid"])]
            # Rotation also reaches this call's independent brief job.
            await manager.run_once()
            await manager.run_once()
            assert [kind for kind, _ in provider.calls] == ["detailed", "brief"]
            assert transcription.documents == []
        finally:
            await manager.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["updated", "deleted", "active", "newer-memory"])
def test_old_inflight_result_is_rejected_if_transcript_or_call_changes(tmp_path, change):
    old = saved_documents(tmp_path, [1])[0]
    store = CallDetailsStore(str(tmp_path / "details"))

    async def run():
        provider, active = DualProvider(), set()
        provider.gates["detailed"] = asyncio.Event()
        manager, transcription = setup_manager(tmp_path, [], store, provider, active_call_ids=lambda: active)
        running = asyncio.create_task(manager.run_once())
        try:
            await asyncio.wait_for(provider.started["detailed"].wait(), 1)
            if change == "updated":
                updated = deepcopy(old)
                updated["segments"][0]["text"] = "The caller corrected the callback date."
                storage.save(manager.settings.transcript_storage_dir, updated)
            elif change == "deleted":
                (tmp_path / "transcripts" / (old["call_sid"] + ".json")).unlink()
            elif change == "active":
                active.add(old["call_sid"])
            else:
                transcription.documents = [old | {"ended_at": None}]
            provider.gates["detailed"].set()
            await running
            record = json.loads((tmp_path / "details" / (old["call_sid"] + ".json")).read_text())
            assert record["summary"] is None
            assert record["summary_job"]["error"] == "transcript-changed"
        finally:
            await manager.close()

    asyncio.run(run())


def test_archive_does_not_bypass_incomplete_memory_copy_or_exhausted_budget(tmp_path):
    incomplete, exhausted, invalid = saved_documents(tmp_path, [1, 2, 3])
    invalid["segments"] = []
    storage.save(str(tmp_path / "transcripts"), invalid)
    store = CallDetailsStore(str(tmp_path / "details"))
    for _ in range(5):
        assert store.begin_summary(exhausted["call_sid"], exhausted)
        assert store.fail_summary(exhausted["call_sid"], exhausted, "provider_unavailable", retry_at=1)
    assert store.set_summary(exhausted["call_sid"], BRIEF, exhausted, kind="brief")

    async def run():
        provider = DualProvider()
        manager, _ = setup_manager(tmp_path, [incomplete | {"ended_at": None}], store, provider)
        try:
            await manager.run_once()
            assert provider.calls == []
            assert store.summary_state(exhausted["call_sid"], exhausted)["attempts"] == 5
        finally:
            await manager.close()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["detailed", "brief"])
def test_retry_cli_reads_an_old_sid_directly_and_preserves_counterpart(tmp_path, monkeypatch, kind):
    old, *_ = saved_documents(tmp_path, range(11))
    store = CallDetailsStore(str(tmp_path / "details"))
    counterpart = "brief" if kind == "detailed" else "detailed"
    assert store.set_summary(old["call_sid"], "Keep this result.", old, kind=counterpart)
    assert store.begin_summary(old["call_sid"], old, kind=kind)
    assert store.fail_summary(old["call_sid"], old, "billing_required", kind=kind)
    settings = SimpleNamespace(transcript_storage_dir=str(tmp_path / "transcripts"),
                               call_details_storage_dir=str(tmp_path / "details"))
    monkeypatch.setattr(commands, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(commands, "Settings", SimpleNamespace(from_env=lambda: settings))
    monkeypatch.setattr(commands.sys, "argv", ["call_details.py", "retry-summary", old["call_sid"], "--kind", kind])
    commands.main()
    reloaded = CallDetailsStore(settings.call_details_storage_dir)
    assert reloaded.summary_state(old["call_sid"], old, kind=kind)["status"] == "missing"
    assert reloaded.summary_state(old["call_sid"], old, kind=counterpart)["status"] == "completed"


def test_authored_summary_cli_can_save_for_an_older_call(tmp_path):
    old, *_ = saved_documents(tmp_path, range(11))
    settings = SimpleNamespace(transcript_storage_dir=str(tmp_path / "transcripts"))
    store = CallDetailsStore(str(tmp_path / "details"))
    text_file = tmp_path / "summary.txt"
    text_file.write_text("An operator's summary for an older archived call.")
    commands.save_summary(settings, store, old["call_sid"], text_file)
    assert store.summary_state(old["call_sid"], old)["status"] == "completed"


def test_corrected_recent_disk_transcript_replaces_stale_hot_summary_input(tmp_path):
    original = saved_documents(tmp_path, [1])[0]
    corrected = deepcopy(original)
    corrected["segments"][0]["text"] = "Please call next Monday instead."
    storage.save(str(tmp_path / "transcripts"), corrected)
    store = CallDetailsStore(str(tmp_path / "details"))
    completed_pair(store, original)

    async def run():
        provider = DualProvider()
        manager, transcription = setup_manager(tmp_path, [original], store, provider)
        transcription.sessions = {}
        transcription.get_saved_call = lambda sid: storage.load_call(manager.settings.transcript_storage_dir, sid)
        try:
            await manager.run_once()
            await manager.run_once()
            assert [kind for kind, _ in provider.calls] == ["detailed", "brief"]
            assert all(item["segments"] == corrected["segments"] for _, item in provider.calls)
            saved = json.loads((tmp_path / "details" / (original["call_sid"] + ".json")).read_text())
            for field in ("summary", "brief_summary"):
                assert saved[field]["fingerprint"] == transcript_fingerprint(corrected)
            assert transcription.documents == [original]
        finally:
            await manager.close()

    asyncio.run(run())
