"""Private caller metadata survives restarts without controlling call routing."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import stat

import pytest

import call_details
from call_details import CallDetailsStore, normalize_caller_number, transcript_fingerprint

CALL = "CA" + "1" * 32
OTHER = "CA" + "2" * 32
START = "2026-09-26T10:00:00+00:00"
END = "2026-09-26T10:00:30+00:00"
NUMBER = "+12025550101"


def session(call_sid=CALL):
    return {"call_sid": call_sid, "stream_sid": "MZ" + "3" * 32, "started_at": START,
            "ended_at": END, "status": "completed", "segments": [
                {"id": "inbound-0", "track": "inbound", "start_ms": 200, "end_ms": 1800,
                 "text": "Please call back tomorrow.", "confidence": .95}],
            "tracks": {"inbound": {"interim": ""}, "outbound": {"interim": ""}}}


def only(store, sessions=None):
    snapshot = store.snapshot(sessions)
    assert len(snapshot["calls"]) == 1
    return snapshot["calls"][0]


def test_private_persistence_restart_and_authoritative_duration(tmp_path):
    root = tmp_path / "private" / "details"
    store = CallDetailsStore(str(root))
    assert store.start(CALL, "+1 (202) 555-0101", START)
    assert store.finish(CALL, END)
    assert only(store)["duration_seconds"] == 30
    assert store.finish(CALL, "2026-09-26T10:01:00Z", duration_seconds=29)
    record = only(store)
    assert record["caller_number"] == NUMBER
    assert record["started_at"] == START and record["ended_at"] == END
    assert record["duration_seconds"] == 29 and record["summary"] is None
    assert store.finish(CALL)  # Capture cleanup must not overwrite CallDuration.
    assert only(store) == record
    restarted = CallDetailsStore(str(root))
    assert only(restarted) == record
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / (CALL + ".json")).stat().st_mode) == 0o600
    assert list(root.glob("*.tmp")) == []
    assert "duration_source" not in record and "schema_version" not in record


def test_duplicate_start_does_not_reset_finished_times_or_caller(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    store.start(CALL, NUMBER, START)
    store.finish(CALL, END, 30)
    before = only(store)
    assert store.start(CALL, "+12025550102", "2026-09-27T10:00:00Z")
    assert only(store) == before


def test_finish_before_start_can_backfill_unknown_caller_without_invented_start(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    assert store.finish(CALL, END, 30)
    record = only(store)
    assert record["started_at"] is None and record["caller_number"] == ""
    assert store.start(CALL, "anonymous")
    assert only(store) == record
    assert store.start(CALL, NUMBER, START)
    record = only(store)
    assert (record["started_at"], record["ended_at"], record["duration_seconds"]) == (START, END, 30)
    assert record["caller_number"] == NUMBER


def test_summary_is_shown_only_for_matching_ended_final_transcript(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    document = session()
    assert store.set_summary(CALL, "Caller requested a callback tomorrow.", document)
    summary = only(store, [document])["summary"]
    assert summary["source"] == "agent" and summary["text"] == "Caller requested a callback tomorrow."
    assert set(summary) == {"text", "source", "created_at"}
    assert only(store)["summary"] is None
    changed = deepcopy(document)
    changed["segments"][0]["text"] = "Corrected transcript."
    assert only(store, [changed])["summary"] is None
    changed = deepcopy(document)
    changed["ended_at"] = None
    assert only(store, [changed])["summary"] is None
    changed = deepcopy(document)
    changed["segments"].append({"track": "outbound", "start_ms": 2000, "end_ms": 3000, "text": "Confirmed."})
    assert only(store, [changed])["summary"] is None
    assert only(CallDetailsStore(str(tmp_path)), [document])["summary"] == summary
    assert store.set_summary(CALL, summary["text"], document)
    assert only(store, [document])["summary"] == summary


def test_fingerprint_ignores_interims_confidence_and_presentation_order():
    original = session()
    changed = deepcopy(original)
    changed["segments"][0]["confidence"] = .01
    changed["segments"][0]["id"] = "different-ui-key"
    changed["tracks"]["inbound"]["interim"] = "not final"
    assert transcript_fingerprint(changed) == transcript_fingerprint(original)
    changed["segments"][0]["start_ms"] += 1
    assert transcript_fingerprint(changed) != transcript_fingerprint(original)
    assert transcript_fingerprint({**original, "segments": []}) is None
    assert transcript_fingerprint({**original, "ended_at": None}) is None


def test_external_helper_summary_refreshes_without_server_restart(tmp_path):
    server = CallDetailsStore(str(tmp_path))
    server.start(CALL, NUMBER, START)
    server.finish(CALL, END, 30)
    assert only(server, [session()])["summary"] is None
    helper = CallDetailsStore(str(tmp_path))
    assert helper.set_summary(CALL, "Callback requested.", session())
    server._last_refresh -= call_details.REFRESH_SECONDS + 1
    assert only(server, [session()])["summary"]["text"] == "Callback requested."
    # A later callback reads this one file even if the catalog cache is fresh.
    assert helper.set_summary(CALL, "Updated callback summary.", session())
    assert server.finish(CALL, duration_seconds=31)
    assert only(server, [session()])["summary"]["text"] == "Updated callback summary."
    assert only(CallDetailsStore(str(tmp_path)), [session()])["duration_seconds"] == 31


def test_external_completed_backfill_does_not_end_an_active_local_call(tmp_path):
    server = CallDetailsStore(str(tmp_path))
    server.start(CALL, NUMBER, START)
    helper = CallDetailsStore(str(tmp_path))
    helper.finish(CALL, END, 30)
    server._last_refresh -= call_details.REFRESH_SECONDS + 1
    assert only(server)["ended_at"] is None
    server.finish(CALL, END, 30)
    assert only(server)["ended_at"] == END


def test_failed_persistence_retains_local_state_and_disk_refresh_cannot_regress_it(tmp_path, monkeypatch):
    store = CallDetailsStore(str(tmp_path))
    store.start(CALL, NUMBER, START)
    original = call_details.os.replace
    monkeypatch.setattr(call_details.os, "replace", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("private-path")))
    assert store.finish(CALL, END, 30) is False
    assert store.snapshot()["storage_error"] == "save-failed"
    assert only(store)["ended_at"] == END
    store._last_refresh -= call_details.REFRESH_SECONDS + 1
    assert only(store)["ended_at"] == END
    assert list(tmp_path.glob("*.tmp")) == []
    monkeypatch.setattr(call_details.os, "replace", original)
    assert store.finish(CALL)
    assert store.snapshot()["storage_error"] == ""
    assert only(CallDetailsStore(str(tmp_path)))["ended_at"] == END


@pytest.mark.parametrize("value,expected", [(NUMBER, NUMBER), (" +1 (202) 555-0101 ", NUMBER),
    ("anonymous", ""), ("2025550101", ""), ("client:private-key", ""), ("+000012345", ""),
    ("+1 2025550101\nsecret", ""), ("+1234567890123456", ""), (None, ""), ({"secret": "value"}, "")])
def test_caller_number_normalization(value, expected):
    assert normalize_caller_number(value) == expected


@pytest.mark.parametrize("sid", ["../private", "", CALL + "/x", "CA" + "g" * 32, None, {}])
def test_invalid_identifiers_are_rejected_without_files(tmp_path, sid):
    store = CallDetailsStore(str(tmp_path))
    assert not store.start(sid, NUMBER)
    assert not store.finish(sid)
    assert not store.set_summary(sid, "Summary", session())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("part", ["root", "ancestor", "file"])
def test_symlink_boundaries_are_not_read_or_written(tmp_path, part):
    actual = tmp_path / "actual" / "details"
    real = CallDetailsStore(str(actual))
    real.start(CALL, NUMBER, START)
    original = (actual / (CALL + ".json")).read_bytes()
    if part == "root":
        root = tmp_path / "linked"
        root.symlink_to(actual, target_is_directory=True)
    elif part == "ancestor":
        root = tmp_path / "linked"
        root.symlink_to(actual.parent, target_is_directory=True)
        root = root / "details"
    else:
        root = tmp_path / "linked"
        root.mkdir()
        (root / (CALL + ".json")).symlink_to(actual / (CALL + ".json"))
    store = CallDetailsStore(str(root))
    assert store.snapshot()["calls"] == []
    assert store.start(CALL, "+12025550101", START) is False
    assert (actual / (CALL + ".json")).read_bytes() == original
    assert store.snapshot()["storage_error"] == "save-failed"


def test_fifo_and_malformed_files_are_skipped_without_blocking(tmp_path):
    os.mkfifo(tmp_path / (CALL + ".json"))
    (tmp_path / (OTHER + ".json")).write_text("not JSON")
    store = CallDetailsStore(str(tmp_path))
    assert store.snapshot()["calls"] == []
    assert not store.finish(CALL, END)
    assert stat.S_ISFIFO((tmp_path / (CALL + ".json")).stat().st_mode)


def test_plain_text_summary_does_not_execute_or_leak_internal_fields(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    assert store.start(CALL, "$(cat private-key)", START)
    text = '<script>example()</script> $(touch not-executed)'
    assert store.set_summary(CALL, text, session())
    record = only(store, [session()])
    assert record["summary"]["text"] == text and record["caller_number"] == ""
    assert not (tmp_path / "not-executed").exists()
    assert "fingerprint" not in json.dumps(record) and "private-key" not in json.dumps(record)
    assert not store.set_summary(CALL, "x\x00y", session())
    assert not store.set_summary(CALL, "x" * 2001, session())
    assert not store.set_summary(OTHER, "Wrong call", session())


def test_unknown_recording_metadata_is_visible_without_invented_caller_or_disk_write(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    recording = {"call_sid": CALL, "started_at": START, "finished_at": END, "duration_seconds": 27.5}
    record = only(store, [recording])
    assert record == {"call_sid": CALL, "caller_number": "", "started_at": START,
                      "ended_at": END, "duration_seconds": 27.5, "summary": None, "brief_summary": None}
    assert list(tmp_path.iterdir()) == []


def test_both_max_length_unicode_summaries_fit_the_bounded_record(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    assert store.set_summary(CALL, "\U0001f4de" * 2000, session())
    assert store.set_summary(CALL, "\U0001f4de" * 280, session(), kind="brief")
    record = only(CallDetailsStore(str(tmp_path)), [session()])
    assert len(record["summary"]["text"]) == 2000
    assert len(record["brief_summary"]["text"]) == 280


def test_recent_ten_retain_relevant_older_call_and_snapshots_are_detached(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    base = datetime(2026, 9, 26, tzinfo=timezone.utc)
    for index in range(13):
        sid = f"CA{index:032x}"
        store.start(sid, NUMBER, (base + timedelta(minutes=index)).isoformat())
        store.finish(sid, (base + timedelta(minutes=index, seconds=10)).isoformat())
    records = store.snapshot()["calls"]
    assert len(records) == 10 and records[0]["call_sid"] == f"CA{12:032x}"
    older = session(f"CA{0:032x}")
    relevant = store.snapshot([older])["calls"]
    assert relevant[0]["call_sid"] == older["call_sid"]
    relevant[0]["caller_number"] = "mutated"
    assert store.snapshot([older])["calls"][0]["caller_number"] == NUMBER


def test_scan_and_memory_bounds_and_disabled_store(tmp_path, monkeypatch):
    store = CallDetailsStore(str(tmp_path))
    for index in range(8): store.start(f"CA{index:032x}", NUMBER, START)
    monkeypatch.setattr(call_details, "MAX_FILES", 3)
    restarted = CallDetailsStore(str(tmp_path))
    assert len(restarted._records) == 3
    assert not restarted.start("CA" + "f" * 32, NUMBER, START)
    disabled = CallDetailsStore("")
    assert not disabled.start(CALL, NUMBER)
    assert not disabled.finish(CALL)
    assert disabled.snapshot([session()]) == {"enabled": False, "storage_error": "", "calls": []}


def test_archive_and_direct_details_bypass_recent_and_memory_scan_caps(tmp_path, monkeypatch):
    store = CallDetailsStore(str(tmp_path))
    base = datetime(2026, 9, 26, tzinfo=timezone.utc)
    for index in range(25):
        sid = f"CA{index:032x}"
        store.start(sid, NUMBER, (base + timedelta(minutes=index)).isoformat())
        store.finish(sid, (base + timedelta(minutes=index, seconds=10)).isoformat())
    assert len(store.snapshot()['calls']) == 10
    monkeypatch.setattr(call_details, 'MAX_FILES', 3)
    restarted = CallDetailsStore(str(tmp_path))
    assert len(restarted._records) == 3
    archive = restarted.archive_snapshot()
    assert len(archive['calls']) == 25
    assert [item['call_sid'] for item in archive['calls']] == [f"CA{i:032x}" for i in reversed(range(25))]
    outside_cache = next(row['call_sid'] for row in archive['calls'] if row['call_sid'] not in restarted._records)
    before = deepcopy(restarted._records)
    assert restarted.get(outside_cache)['caller_number'] == NUMBER
    assert restarted._records == before
    assert restarted.get('CA' + 'f' * 32) is None
    archive['calls'][0]['caller_number'] = 'mutated'
    assert restarted.archive_snapshot()['calls'][0]['caller_number'] == NUMBER


def test_archive_sort_is_stable_for_equal_timestamps_and_local_writes_invalidate_cache(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    for index in (3, 1, 2):
        sid = f"CA{index:032x}"
        store.start(sid, NUMBER, START)
        store.finish(sid, END)
    assert [row['call_sid'] for row in store.archive_snapshot()['calls']] == [f"CA{i:032x}" for i in (3, 2, 1)]
    store.start('CA' + 'f' * 32, NUMBER, START)
    assert len(store.archive_snapshot()['calls']) == 4


def test_direct_details_summary_requires_matching_full_transcript(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    document = session()
    assert store.set_summary(CALL, 'Please call back.', document)
    assert store.get(CALL)['summary'] is None
    assert store.archive_snapshot()['calls'][0]['summary'] is None
    assert store.get(CALL, document)['summary']['text'] == 'Please call back.'
    stale = deepcopy(document)
    stale['segments'][0]['text'] = 'Different message.'
    assert store.get(CALL, stale)['summary'] is None
    assert store.get(CALL, session(OTHER))['summary'] is None
    assert 'fingerprint' not in json.dumps(store.get(CALL, document))


def test_archive_keeps_failed_local_write_and_live_ownership(tmp_path, monkeypatch):
    store = CallDetailsStore(str(tmp_path))
    store.start(CALL, NUMBER, START)
    external = CallDetailsStore(str(tmp_path))
    external.finish(CALL, END, 27)
    assert store.get(CALL)['ended_at'] is None
    assert store.archive_snapshot()['calls'][0]['ended_at'] is None
    def fail(*args, **kwargs):
        raise OSError('fixture')
    monkeypatch.setattr(call_details.os, 'replace', fail)
    assert not store.finish(CALL, END, 29)
    assert store.get(CALL)['duration_seconds'] == 29
    archive = store.archive_snapshot()
    assert archive['storage_error'] == 'save-failed'
    assert archive['calls'][0]['duration_seconds'] == 29


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'malformed', 'oversized', 'wrong-sid'])
def test_archive_and_direct_details_skip_invalid_files_without_mutation(tmp_path, kind):
    root = tmp_path / 'details'
    root.mkdir()
    target = root / (CALL + '.json')
    if kind == 'symlink':
        outside = tmp_path / 'private.json'
        outside.write_text('{"private":"not-a-call"}')
        target.symlink_to(outside)
    elif kind == 'fifo':
        os.mkfifo(target)
    elif kind == 'malformed':
        target.write_text('{invalid')
    elif kind == 'oversized':
        target.write_text('x' * (call_details.MAX_FILE_BYTES + 1))
    else:
        target.write_text(json.dumps(call_details._blank(OTHER)))
    store = CallDetailsStore(str(root))
    assert store.get(CALL) is None
    assert store.archive_snapshot()['calls'] == []
    assert store.archive_snapshot()['storage_error'] == 'load-failed'
    assert target.exists()


def test_threaded_callbacks_do_not_lose_caller_or_completed_state(tmp_path):
    store = CallDetailsStore(str(tmp_path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.start(CALL, NUMBER, START), range(16)))
    assert all(results)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.finish(CALL, END, 30), range(16)))
    assert all(results)
    record = only(store)
    assert record["caller_number"] == NUMBER and record["ended_at"] == END
    assert only(CallDetailsStore(str(tmp_path))) == record


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "30", 86401])
def test_invalid_durations_fail_open_without_ending_call(tmp_path, value):
    store = CallDetailsStore(str(tmp_path))
    store.start(CALL, NUMBER, START)
    assert not store.finish(CALL, END, value)
    assert only(store)["ended_at"] is None
