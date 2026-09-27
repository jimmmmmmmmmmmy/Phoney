"""Saved calls survive hot-cache eviction, restarts, and paginated navigation."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from call_details import CallDetailsStore
from call_history import PAGE_SIZE
from dashboard import register_dashboard
from transcription import TranscriptionManager
from transcription import storage
from call_history import page, saved_call
from test_transcription import settings
from test_call_details import session, NUMBER
from test_recording_playback import library, write_capture


def sid(number):
    return "CA" + f"{number:032x}"


def document(number, *, minute=None):
    result = session(sid(number))
    start = datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=number if minute is None else minute)
    result.update(schema_version=1, provider="deepgram", model="nova-3", finish_reason="", storage_error="",
                  started_at=start.isoformat(), ended_at=(start + timedelta(seconds=30)).isoformat())
    result["segments"][0]["text"] = f"Saved conversation {number}."
    result["tracks"] = {name: {"meaning": meaning, "status": "completed", "error": "", "interim": ""}
                        for name, meaning in (("inbound", "caller-input"), ("outbound", "caller-playback"))}
    return result


def seed(root, count=45):
    store = CallDetailsStore(str(root / "details"))
    for number in range(1, count + 1):
        doc = document(number)
        storage.save(str(root / "transcripts"), doc)
        assert store.start(doc["call_sid"], NUMBER, doc["started_at"])
        assert store.finish(doc["call_sid"], doc["ended_at"], 30)
        assert store.set_summary(doc["call_sid"], f"Summary {number}", doc)
        assert store.set_summary(doc["call_sid"], f"Brief {number}", doc, kind="brief")


def viewer(root):
    manager = TranscriptionManager(settings(root / "transcripts"))
    details = CallDetailsStore(str(root / "details"))
    recordings = library(root / "recordings")
    app = FastAPI()
    register_dashboard(app, manager.settings, manager, recording_library=recordings, call_details_store=details)
    return TestClient(app), manager, details, recordings


def test_oldest_call_direct_transcript_summary_export_and_audio_after_restart(tmp_path):
    seed(tmp_path)
    write_capture(tmp_path / "recordings", sid=sid(1))
    client, manager, _, _ = viewer(tmp_path)
    assert sid(1) not in {item["call_sid"] for item in manager.snapshot()["sessions"]}
    with client:
        result = client.get("/api/transcripts", params={"call_sid": sid(1)}).json()
        assert result["selected_call_sid"] == sid(1)
        assert result["history"]["total"] == 45
        assert len(result["sessions"]) == PAGE_SIZE + 1
        chosen = next(item for item in result["sessions"] if item["call_sid"] == sid(1))
        assert chosen["segments"][0]["text"] == "Saved conversation 1."
        assert all(not row["segments"] for row in result["sessions"] if row is not chosen)
        details = next(item for item in result["call_details"]["calls"] if item["call_sid"] == sid(1))
        assert details["caller_number"] == NUMBER
        assert details["summary"]["text"] == "Summary 1"
        assert details["brief_summary"]["text"] == "Brief 1"
        exported = client.get(f"/api/transcripts/{sid(1)}/export").json()
        assert exported["call_details"]["summary"]["text"] == "Summary 1"
        assert exported["session"]["segments"] == chosen["segments"]
        assert "Saved conversation 1." in client.get(f"/api/transcripts/{sid(1)}/export?format=txt").text
        recording = next(item for item in result["recordings"]["recordings"] if item["call_sid"] == sid(1))
        assert client.get(recording["url"], headers={"Range": "bytes=0-43"}).status_code == 206
        assert len(manager.history) == storage.MAX_HISTORY


def test_all_pages_have_caller_metrics_and_summaries_independent_of_visible_page(tmp_path):
    seed(tmp_path)
    client, _, _, _ = viewer(tmp_path)
    seen, cursor = [], None
    with client:
        while True:
            payload = client.get("/api/transcripts", params={"caller": NUMBER, **({"cursor": cursor} if cursor else {})}).json()
            assert payload["history"]["total"] == 45
            assert payload["history"]["duration_seconds"] == 45 * 30
            assert payload["history"]["caller_metrics"][NUMBER]["total"] == 45
            assert payload["history"]["caller_metrics"][NUMBER]["duration_seconds"] == 45 * 30
            assert payload["history"]["complete"] is True
            assert len(payload["sessions"]) <= PAGE_SIZE
            ids = [row["call_sid"] for row in payload["sessions"]]
            assert {row["call_sid"] for row in payload["call_details"]["calls"]} == set(ids)
            assert all(row["brief_summary"] for row in payload["call_details"]["calls"])
            seen.extend(ids)
            cursor = payload["history"]["next_cursor"]
            if not payload["history"]["has_more"]:
                assert cursor is None
                break
        assert seen == [sid(number) for number in range(45, 0, -1)]
        empty = client.get("/api/transcripts", params={"caller": "+12025550999"}).json()
        assert empty["sessions"] == [] and empty["history"]["total"] == 0


def test_keyset_cursor_survives_new_calls_and_equal_timestamps(tmp_path):
    for number in range(1, 42):
        storage.save(str(tmp_path / "transcripts"), document(number, minute=1))
    client, manager, _, _ = viewer(tmp_path)
    with client:
        first = client.get("/api/transcripts").json()
        storage.save(str(tmp_path / "transcripts"), document(99, minute=2))
        manager.archive._refreshed = float("-inf")
        second = client.get("/api/transcripts", params={"cursor": first["history"]["next_cursor"]}).json()
        third = client.get("/api/transcripts", params={"cursor": second["history"]["next_cursor"]}).json()
        assert [row["call_sid"] for result in (first, second, third) for row in result["sessions"]] == [sid(n) for n in range(41, 0, -1)]


def test_missing_call_never_opens_another_call_and_corrupt_transcript_keeps_recording(tmp_path):
    seed(tmp_path, 25)
    write_capture(tmp_path / "recordings", sid=sid(1))
    (tmp_path / "transcripts" / (sid(1) + ".json")).write_text("{broken")
    client, _, _, _ = viewer(tmp_path)
    with client:
        result = client.get("/api/transcripts", params={"call_sid": sid(999)}).json()
        assert result["selected_call_sid"] is None and result["selected_call_missing"]
        assert all(not row["segments"] for row in result["sessions"])
        assert client.get(f"/api/transcripts/{sid(999)}/export").status_code == 404
        result = client.get("/api/transcripts", params={"call_sid": sid(1)}).json()
        assert result["selected_call_sid"] == sid(1) and not result["selected_call_missing"]
        assert sid(1) not in {row["call_sid"] for row in result["sessions"]}
        assert any(row["call_sid"] == sid(1) for row in result["recordings"]["recordings"])
        assert client.get(f"/api/transcripts/{sid(1)}/export").status_code == 404


def test_cursor_and_caller_validation(tmp_path):
    seed(tmp_path, 25)
    client, _, _, _ = viewer(tmp_path)
    with client:
        cursor = client.get("/api/transcripts").json()["history"]["next_cursor"]
        for params in ({"cursor": "!"}, {"cursor": "a" * 513}, {"cursor": "W10"},
                       {"cursor": cursor, "caller": NUMBER}, {"caller": "invalid"}):
            assert client.get("/api/transcripts", params=params).status_code == 400


def test_direct_file_reader_and_index_reject_bad_files_and_refresh_replacements(tmp_path):
    root = tmp_path / "transcripts"
    storage.save(str(root), document(1))
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(document(2)))
    (root / (sid(2) + ".json")).symlink_to(outside)
    os.mkfifo(root / (sid(3) + ".json"))
    (root / (sid(4) + ".json")).write_text(json.dumps({**document(4), "segments": "invalid"}))
    (root / (sid(5) + ".json")).write_text(json.dumps(document(6)))
    for invalid in (sid(2), sid(3), sid(5), "../outside", ""):
        assert storage.load_call(str(root), invalid) is None
    manager = TranscriptionManager(settings(root))
    assert manager.archive_snapshot()["sessions"][0]["call_sid"] == sid(1)
    assert len(manager.archive_snapshot()["sessions"]) == 1
    assert manager.get_saved_call(sid(4)) is None
    changed = document(1, minute=500)
    storage.save(str(root), changed)
    manager.archive._refreshed = float("-inf")
    assert manager.archive_snapshot()["sessions"][0]["started_at"] == changed["started_at"]
    assert not manager.archive_snapshot()["sessions"][0]["segments"]


def test_archive_not_truncated_by_non_call_files_or_scan_budget(tmp_path, monkeypatch):
    root = tmp_path / "transcripts"
    storage.save(str(root), document(1))
    storage.save(str(root), document(2))
    monkeypatch.setattr(storage, "MAX_SCAN", 1)
    assert storage.list_call_ids(str(root)) == [sid(1), sid(2)]
    manager = TranscriptionManager(settings(root))
    assert len(manager.archive_snapshot()["sessions"]) == 2


def test_transcript_only_duration_uses_elapsed_time(tmp_path):
    storage.save(str(tmp_path / "transcripts"), document(1))
    client, _, _, _ = viewer(tmp_path)
    with client:
        payload = client.get("/api/transcripts").json()
        assert payload["history"]["duration_seconds"] == 30
        assert payload["call_details"]["calls"][0]["duration_seconds"] == 30


def test_ended_hot_calls_follow_disk_corrections_but_live_and_failed_saves_remain(tmp_path):
    root = tmp_path / "transcripts"
    doc = document(1)
    storage.save(str(root), doc)
    manager = TranscriptionManager(settings(root))
    changed = deepcopy(doc)
    changed["segments"][0]["text"] = "Corrected saved text."
    storage.save(str(root), changed)
    assert saved_call(manager, sid(1), manager.snapshot()["sessions"]) == changed
    (root / (sid(1) + ".json")).unlink()
    assert saved_call(manager, sid(1), manager.snapshot()["sessions"]) is None
    manager.sessions[sid(1)] = object()  # A finishing call can already have ended_at.
    assert saved_call(manager, sid(1), [doc]) == doc
    manager.sessions.clear()
    doc["storage_error"] = "storage-failed"
    assert saved_call(manager, sid(1), [doc]) == doc


def test_caller_filter_preserves_other_incoming_call_alerts_without_counting_them(tmp_path):
    seed(tmp_path, 25)
    client, manager, store, recordings = viewer(tmp_path)
    live = {**document(100), "ended_at": None, "status": "live"}
    store.start(sid(100), "+12025550999", live["started_at"])
    snapshot = manager.snapshot()
    snapshot["sessions"].insert(0, live)
    result = page(manager, store, recordings, snapshot,
                  {"enabled": False, "voicemails": []}, caller=NUMBER)
    assert result["history"]["total"] == 25
    assert result["history"]["duration_seconds"] == 750
    assert len(result["sessions"]) == PAGE_SIZE + 1
    assert any(item["call_sid"] == sid(100) and not item["ended_at"] for item in result["sessions"])


def test_corrupt_transcripts_mark_partial_history(tmp_path):
    root = tmp_path / "transcripts"
    storage.save(str(root), {"call_sid": sid(1), "schema_version": 1})
    client, _, _, _ = viewer(tmp_path)
    with client:
        assert client.get("/api/transcripts").json()["history"]["complete"] is False


def test_old_voicemail_keeps_its_collection_metadata_and_export(tmp_path):
    from voicemail import VoicemailStore
    from test_voicemail_archive import receipt, settings as voicemail_settings
    root = tmp_path / "voicemail"
    for number in range(1, 26):
        storage.save(str(root), receipt(number))
    storage.save(str(tmp_path / "transcripts"), document(1))
    manager = TranscriptionManager(settings(tmp_path / "transcripts"))
    voicemail = VoicemailStore(voicemail_settings(root))
    app = FastAPI()
    register_dashboard(app, manager.settings, manager, voicemail_store=voicemail)
    with TestClient(app) as client:
        payload = client.get("/api/transcripts", params={"call_sid": sid(1)}).json()
        assert payload["history"]["total"] == 25
        assert payload["selected_call_sid"] == sid(1)
        assert any(row["call_sid"] == sid(1) for row in payload["voicemail"]["voicemails"])
        exported = client.get(f"/api/transcripts/{sid(1)}/export").json()
        assert exported["voicemail"]["reason"] == "no-answer"
