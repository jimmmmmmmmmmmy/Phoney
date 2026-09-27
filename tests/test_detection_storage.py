"""Advisory persistence is bounded, private, restartable, and fails open."""

import json
import os
import stat

import pytest

from partner_detection import storage
from partner_detection.storage import DetectionStore

CALL = "CA" + "1" * 32
OTHER = "CA" + "2" * 32


def result(**changes):
    return {"provider": "modulate", "status": "complete", "label": "synthetic",
            "confidence": .93, "reason": "confident_synthetic", "streams": 1,
            "observations": 2, "accepted_frames": 200, "dropped_frames": 0,
            "submitted_audio_ms": 4000, "coverage_limited": False, **changes}


def test_private_atomic_persistence_and_detached_public_results(tmp_path):
    root = tmp_path / "private" / "detection"
    store = DetectionStore(str(root))
    supplied = result()
    assert store.save(CALL, supplied)
    supplied["label"] = "changed"
    saved = store.get(CALL)
    assert saved["label"] == "synthetic"
    assert set(saved) == storage.RESULT_FIELDS | {"call_sid", "updated_at"}
    assert saved == DetectionStore(str(root)).get(CALL)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / (CALL + ".json")).stat().st_mode) == 0o600
    assert list(root.glob("*.tmp")) == []
    saved["confidence"] = .1
    snapshot = store.snapshot()
    snapshot["calls"][0]["confidence"] = .2
    assert store.get(CALL)["confidence"] == .93
    assert snapshot["storage_error"] == ""


def test_restart_abstains_on_interrupted_analysis_but_live_owner_keeps_analyzing(tmp_path):
    store = DetectionStore(str(tmp_path))
    pending = result(status="analyzing", label="unknown", confidence=None, reason="analyzing")
    assert store.save(CALL, pending)
    assert store.get(CALL)["status"] == "analyzing"
    recovered = DetectionStore(str(tmp_path))
    assert recovered.get(CALL)["status"] == "unknown"
    assert recovered.get(CALL)["reason"] == "interrupted"
    recovered._last_refresh -= 3
    assert recovered.snapshot()["calls"][0]["reason"] == "interrupted"
    # Recovery is advisory only; candidates never write another process's files.
    assert json.loads((tmp_path / (CALL + ".json")).read_text())["status"] == "analyzing"


def test_late_start_does_not_replace_final_but_new_epoch_can_restart(tmp_path):
    store = DetectionStore(str(tmp_path))
    assert store.save(CALL, result())
    pending = result(status="analyzing", label="unknown", confidence=None, reason="analyzing")
    assert store.save(CALL, pending)
    assert store.get(CALL)["status"] == "complete"
    assert store.save(CALL, {**pending, "streams": 2})
    assert store.get(CALL)["status"] == "analyzing"


@pytest.mark.parametrize("change", [
    {"api_key": "must-not-be-saved"}, {"frames": ["raw audio"]}, {"provider": "unknown"},
    {"confidence": float("nan")}, {"confidence": float("inf")}, {"confidence": True},
    {"confidence": 1.01}, {"confidence": -.01}, {"reason": "https://provider/?key=secret"},
    {"reason": ["confident_synthetic"]}, {"status": "human"}, {"label": "human"},
    {"accepted_frames": -1}, {"accepted_frames": True}, {"observations": 1.5},
    {"streams": storage.MAX_COUNTER + 1}, {"submitted_audio_ms": 3_600_001},
    {"coverage_limited": 1}, {"status": "unknown"}, {"confidence": None},
])
def test_invalid_fields_and_private_payloads_never_reach_disk(tmp_path, change):
    store = DetectionStore(str(tmp_path))
    assert not store.save(CALL, result(**change))
    assert store.get(CALL) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("sid", [None, "", "../secret", "CA" + "1" * 32 + "/file", "CAwrong", 7])
def test_invalid_and_unknown_identifiers_are_safe(tmp_path, sid):
    store = DetectionStore(str(tmp_path))
    assert not store.save(sid, result())
    assert store.get(sid) is None
    assert store.get(OTHER) is None
    assert list(tmp_path.iterdir()) == []


def test_disabled_or_relative_storage_cannot_create_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    disabled = DetectionStore("")
    assert not disabled.save(CALL, result())
    assert disabled.snapshot() == {"enabled": False, "storage_error": "", "calls": []}
    relative = DetectionStore("relative")
    assert not relative.save(CALL, result())
    assert relative.storage_error == "storage-unavailable"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("part", ["root", "parent", "file", "hardlink"])
def test_symlinks_and_hardlinks_cannot_cross_storage_boundary(tmp_path, part):
    actual = tmp_path / "actual"
    actual.mkdir()
    outside = actual / "outside.json"
    original = b'{"private":"unchanged"}'
    outside.write_bytes(original)
    root = tmp_path / "detection"
    if part == "root":
        root.symlink_to(actual, target_is_directory=True)
    elif part == "parent":
        root.symlink_to(actual, target_is_directory=True)
        root = root / "child"
    else:
        root.mkdir()
        target = root / (CALL + ".json")
        if part == "file":
            target.symlink_to(outside)
        else:
            os.link(outside, target)
    store = DetectionStore(str(root))
    assert store.get(CALL) is None
    assert not store.save(CALL, result())
    assert store.storage_error == "storage-unavailable"
    assert outside.read_bytes() == original
    assert not (actual / "child").exists()


def test_malformed_oversized_fifo_and_extra_field_files_are_skipped(tmp_path):
    store = DetectionStore(str(tmp_path))
    assert store.save(CALL, result())
    valid = json.loads((tmp_path / (CALL + ".json")).read_text())
    for index, payload in enumerate(("not json", "x" * (storage.MAX_FILE_BYTES + 1),
                                    json.dumps({**valid, "call_sid": OTHER, "api_key": "private"})), 2):
        (tmp_path / ("CA" + str(index) * 32 + ".json")).write_text(payload)
    os.mkfifo(tmp_path / ("CA" + "5" * 32 + ".json"))
    restarted = DetectionStore(str(tmp_path))
    assert [record["call_sid"] for record in restarted.snapshot()["calls"]] == [CALL]


def test_failed_write_retains_advisory_and_cannot_regress_on_disk_refresh(tmp_path, monkeypatch):
    store = DetectionStore(str(tmp_path))
    assert store.save(CALL, result(status="analyzing", label="unknown", confidence=None, reason="analyzing"))
    def failed(*args, **kwargs):
        raise OSError("contains private details")
    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "replace", failed)
        assert not store.save(CALL, result())
    store._last_refresh -= 3
    assert store.snapshot()["storage_error"] == "storage-unavailable"
    assert store.get(CALL)["status"] == "complete"
    assert not list(tmp_path.glob("*.tmp"))
    assert store.save(CALL, result())
    assert store.storage_error == ""


def test_memory_and_public_snapshot_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "MAX_FILES", 3)
    monkeypatch.setattr(storage, "MAX_PUBLIC_RESULTS", 2)
    store = DetectionStore(str(tmp_path))
    for index in range(6):
        assert store.save("CA" + str(index) * 32, result())
    assert len(store._records) == 3
    assert [item["call_sid"] for item in store.snapshot()["calls"]] == ["CA" + "5" * 32, "CA" + "4" * 32]


def test_get_refreshes_a_completed_result_from_another_process(tmp_path):
    store = DetectionStore(str(tmp_path))
    other = DetectionStore(str(tmp_path))
    assert other.save(CALL, result())
    assert store.get(CALL)["label"] == "synthetic"


def analysis(start=0, *, complete=True):
    from partner_detection.analysis import build_analysis
    return build_analysis([dict(stream_id="MZ-one", start_ms=start, end_ms=start + 4000,
                                verdict="synthetic", confidence=.94)], complete=complete)


def test_version_two_windows_survive_restart_and_catalog_remains_compact(tmp_path):
    store = DetectionStore(str(tmp_path))
    evidence = analysis()
    assert store.save(CALL, result(analysis=evidence))
    stored = json.loads((tmp_path / (CALL + ".json")).read_text())
    assert stored["schema_version"] == 2
    assert stored["analysis"]["windows"] == evidence["windows"]
    assert stored["analysis"]["synthetic_intervals"] == evidence["synthetic_intervals"]
    restarted = DetectionStore(str(tmp_path))
    assert restarted.get(CALL)["analysis"] == evidence
    catalog = restarted.snapshot()["calls"][0]
    assert "windows" not in catalog["analysis"]
    assert "synthetic_intervals" not in catalog["analysis"]
    assert catalog["analysis"]["alert"] == "ai_detected"
    catalog["analysis"]["alert"] = "none"
    assert restarted.get(CALL)["analysis"]["alert"] == "ai_detected"


def test_interrupted_live_windows_remain_partial_evidence_after_restart(tmp_path):
    store = DetectionStore(str(tmp_path))
    pending = result(status="analyzing", label="unknown", confidence=None, reason="analyzing",
                     analysis=analysis(complete=False))
    assert store.save(CALL, pending)
    restored = DetectionStore(str(tmp_path)).get(CALL)
    assert restored["status"] == "unknown"
    assert restored["reason"] == "interrupted"
    assert restored["analysis"]["complete"] is False
    assert restored["analysis"]["alert"] == "ai_detected"
    assert len(restored["analysis"]["windows"]) == 1
    assert len(restored["analysis"]["synthetic_intervals"]) == 1


def test_full_window_cache_is_bounded_and_evicted_details_reload_on_demand(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "MAX_DETAIL_RECORDS", 2)
    store = DetectionStore(str(tmp_path))
    for index in range(6):
        assert store.save("CA" + str(index) * 32, result(analysis=analysis()))
    assert len(store._details) == 2
    assert all("windows" not in value["analysis"] for value in store._records.values())
    assert all("synthetic_intervals" not in value["analysis"] for value in store._records.values())
    assert len(store.get("CA" + "0" * 32)["analysis"]["windows"]) == 1
    assert len(store.get("CA" + "0" * 32)["analysis"]["synthetic_intervals"]) == 1
    assert len(store._details) == 2
    restarted = DetectionStore(str(tmp_path))
    assert len(restarted._details) == 2
    assert len(restarted._records) == 6


def test_oldest_saved_result_is_readable_even_when_it_is_immediately_evicted(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "MAX_FILES", 2)
    monkeypatch.setattr(storage, "MAX_DETAIL_RECORDS", 2)
    store = DetectionStore(str(tmp_path))
    sids = ["CA" + str(index) * 32 for index in range(4)]
    for sid in sids:
        assert store.save(sid, result(analysis=analysis()))
    assert sids[0] not in store._records
    assert (tmp_path / (sids[0] + ".json")).is_file()
    for current in (store, DetectionStore(str(tmp_path))):
        # Fill the cache with newer calls regardless of directory enumeration.
        for sid in sids[2:]:
            assert current.get(sid)
        saved = current.get(sids[0])
        assert saved["analysis"]["alert"] == "ai_detected"
        assert saved["analysis"]["windows"] == analysis()["windows"]
        assert saved["analysis"]["synthetic_intervals"] == analysis()["synthetic_intervals"]
        assert sids[0] not in current._records
        assert len(current._records) <= 2 and len(current._details) <= 2
        saved["analysis"]["windows"].clear()
        assert current.get(sids[0])["analysis"]["windows"]


def test_uncached_interrupted_archive_result_keeps_recovery_semantics(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "MAX_FILES", 1)
    store = DetectionStore(str(tmp_path))
    pending = result(status="analyzing", label="unknown", confidence=None, reason="analyzing",
                     analysis=analysis(complete=False))
    assert store.save(CALL, pending)
    assert store.save(OTHER, result(analysis=analysis()))
    recovered = DetectionStore(str(tmp_path))
    assert recovered.get(OTHER)
    older = recovered.get(CALL)
    assert older["status"] == "unknown" and older["reason"] == "interrupted"
    assert older["analysis"]["complete"] is False
    assert older["analysis"]["synthetic_intervals"]
    assert json.loads((tmp_path / (CALL + ".json")).read_text())["status"] == "analyzing"


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_analysis_versions_remain_readable_without_interval_migration(tmp_path, version):
    from partner_detection.analysis import build_analysis
    evidence = build_analysis(analysis()["windows"], version=version)
    store = DetectionStore(str(tmp_path))
    assert store.save(CALL, result(analysis=evidence))
    assert DetectionStore(str(tmp_path)).get(CALL)["analysis"] == evidence
    assert "synthetic_intervals" not in evidence


def test_analysis_tampering_and_extra_payloads_are_rejected(tmp_path):
    store = DetectionStore(str(tmp_path))
    for evidence in (analysis() | {"synthetic_share": .93}, analysis() | {"secret": "private"}):
        assert not store.save(CALL, result(analysis=evidence))
    assert not list(tmp_path.iterdir())


def test_unchanged_large_documents_are_not_reparsed_on_catalog_poll(tmp_path, monkeypatch):
    store = DetectionStore(str(tmp_path))
    assert store.save(CALL, result(analysis=analysis()))
    restarted = DetectionStore(str(tmp_path))
    def unexpected_read(*args, **kwargs):
        raise AssertionError("Unchanged catalog entry was reparsed")
    monkeypatch.setattr(restarted, "_read", unexpected_read)
    restarted._last_refresh -= 3
    assert restarted.snapshot()["calls"][0]["call_sid"] == CALL
