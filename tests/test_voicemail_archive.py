"""Legacy voicemail receipts remain discoverable outside the hot history."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from voicemail import MAX_RECEIPT_BYTES, VoicemailStore
from transcription import storage


def settings(root, enabled=True):
    return SimpleNamespace(voicemail_enabled=enabled, voicemail_storage_dir=str(root),
                           voicemail_max_seconds=120)


def receipt(index):
    return {"schema_version": 1, "call_sid": f"CA{index:032x}", "mode": "voicemail_stub",
            "reason": "no-answer", "started_at": "2026-07-02T12:00:00+00:00",
            "ended_at": "2026-07-02T12:01:00+00:00", "recording_status": "completed",
            "recording_sid": f"RE{index:032x}", "duration_seconds": 60, "storage_error": ""}


def test_archive_and_get_bypass_recent_and_directory_scan_caps(tmp_path, monkeypatch):
    for index in range(25):
        storage.save(str(tmp_path), receipt(index))
    store = VoicemailStore(settings(tmp_path))
    assert len(store.snapshot()['voicemails']) == 10
    monkeypatch.setattr(storage, 'MAX_SCAN', 2)
    fresh = VoicemailStore(settings(tmp_path))
    assert len(fresh.snapshot()['voicemails']) == 2
    snapshot = fresh.archive_snapshot()
    assert snapshot['storage_error'] == ''
    assert [row['call_sid'] for row in snapshot['voicemails']] == [f"CA{i:032x}" for i in reversed(range(25))]
    sid = f"CA{0:032x}"
    assert fresh.get(sid) == receipt(0)
    assert len(fresh.records) == 2
    assert fresh.get('../secret') is None
    assert fresh.get(f"CA{26:032x}") is None
    snapshot['voicemails'][0]['reason'] = 'changed'
    assert fresh.archive_snapshot()['voicemails'][0]['reason'] == 'no-answer'
    assert str(tmp_path) not in json.dumps(snapshot)


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'malformed', 'oversized', 'wrong-sid'])
def test_archive_invalid_receipts_are_safe_and_mark_partial_history(tmp_path, kind):
    root = tmp_path / 'voicemails'
    root.mkdir()
    target = root / (receipt(0)['call_sid'] + '.json')
    if kind == 'symlink':
        outside = tmp_path / 'private.json'
        outside.write_text(json.dumps(receipt(0)))
        target.symlink_to(outside)
    elif kind == 'fifo':
        os.mkfifo(target)
    elif kind == 'malformed':
        target.write_text('{broken')
    elif kind == 'oversized':
        target.write_text(' ' * (MAX_RECEIPT_BYTES + 1))
    else:
        target.write_text(json.dumps(receipt(1)))
    storage.save(str(root), receipt(2))
    store = VoicemailStore(settings(root))
    assert store.get(receipt(0)['call_sid']) is None
    snapshot = store.archive_snapshot()
    assert snapshot['storage_error'] == 'load-failed'
    assert snapshot['voicemails'] == [receipt(2)]
    assert target.exists()


def test_pending_and_failed_writes_remain_authoritative(tmp_path, monkeypatch):
    async def run():
        store = VoicemailStore(settings(tmp_path))
        sid = receipt(0)['call_sid']
        assert store.start(sid, 'no-answer')
        assert store.get(sid)['recording_status'] == 'awaiting'
        assert store.archive_snapshot()['voicemails'][0]['ended_at'] is None
        store.recording(sid, receipt(0)['recording_sid'], 'completed', 20)
        assert store.get(sid)['duration_seconds'] == 20
        def fail(*args):
            raise OSError('private-path')
        monkeypatch.setattr(storage, 'save', fail)
        await store.close()
        snapshot = store.archive_snapshot()
        assert snapshot['storage_error'] == 'save-failed'
        assert snapshot['voicemails'][0]['duration_seconds'] == 20
        assert store.get(sid)['storage_error'] == 'save-failed'
        assert 'private-path' not in json.dumps(snapshot)
    asyncio.run(run())


def test_get_revalidates_completed_receipt_after_loading(tmp_path):
    record = receipt(0)
    storage.save(str(tmp_path), record)
    store = VoicemailStore(settings(tmp_path))
    assert store.get(record['call_sid']) == record
    (tmp_path / (record['call_sid'] + '.json')).write_text('{broken')
    assert store.get(record['call_sid']) is None
    assert store.archive_snapshot()['storage_error'] == 'load-failed'


def test_archive_empty_disabled_and_symlinked_root_are_safe(tmp_path):
    missing = tmp_path / 'not-created'
    store = VoicemailStore(settings(missing))
    assert store.archive_snapshot() == {'enabled': True, 'storage_error': '', 'voicemails': []}
    assert not missing.exists()
    disabled = VoicemailStore(settings(tmp_path, enabled=False))
    assert disabled.archive_snapshot() == {'enabled': False, 'storage_error': '', 'voicemails': []}
    assert disabled.get(receipt(0)['call_sid']) is None
    target = tmp_path / 'private'
    target.mkdir()
    storage.save(str(target), receipt(0))
    link = tmp_path / 'linked'
    link.symlink_to(target, target_is_directory=True)
    linked = VoicemailStore(settings(link))
    assert linked.get(receipt(0)['call_sid']) is None
    assert linked.archive_snapshot() == {'enabled': True, 'storage_error': 'load-failed', 'voicemails': []}
