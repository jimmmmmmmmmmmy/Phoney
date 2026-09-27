"""Durable workspace imports and concurrent writes preserve shared records."""

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import replace
import json
import multiprocessing
import os
import sqlite3
import stat

import pytest

from config import Settings
from workspace_store import (DATABASE_NAME, DEMO_PHONES, WorkspaceConflict, WorkspaceError,
                             WorkspaceStore, WorkspaceUnavailable)


def contact(index=1, **changes):
    return {"id": f"local-contact-{index:08d}", "firstName": "Sample", "lastName": "Person",
            "phone": f"+1941666{index:04d}", "createdAt": "2026-09-26T15:00:00Z",
            "email": "sample@example.com", "address": "", "website": "", "company": "",
            "status": "New", "labels": ["Customers"], "demo": False, **changes}


def agent(index=1, **changes):
    return {"id": f"agent-draft-{index:08d}", "name": f"Reception {index}",
            "prompt": "Ask how we can help.", "createdAt": "2026-09-26T15:00:00Z", **changes}


def payload(**changes):
    return {"contacts": [], "demoOverrides": [], "agents": [], **changes}


def test_restart_and_two_existing_instances_see_atomic_record_edits(tmp_path):
    directory = str(tmp_path / "workspace")
    first, second = WorkspaceStore(directory), WorkspaceStore(directory)
    assert first.put_contact(contact()["id"], contact()) == {**contact(), "revision": 1}
    assert second.snapshot()["contacts"] == [{**contact(), "revision": 1}]
    updated = contact(firstName="Updated", labels=["Legal"], revision=1)
    second.put_contact(updated["id"], updated)
    second.put_agent(agent()["id"], agent())
    assert WorkspaceStore(directory).snapshot() == {
        "version": 1, "contacts": [{**updated, "revision": 2}], "demoOverrides": [], "agents": [{**agent(), "revision": 1}]}


def test_database_and_directory_are_private_even_if_preexisting(tmp_path):
    directory = tmp_path / "workspace"
    directory.mkdir(mode=0o755)
    database = directory / DATABASE_NAME
    database.touch(mode=0o644)
    store = WorkspaceStore(str(directory))
    store.put_contact(contact()["id"], contact())
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert set(p.name for p in directory.iterdir()) == {DATABASE_NAME}


def test_two_store_instances_serialize_concurrent_distinct_writes(tmp_path):
    directory = str(tmp_path / "workspace")

    def save(index):
        record = contact(index)
        return WorkspaceStore(directory).put_contact(record["id"], record)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(save, range(1, 33)))
    assert len(results) == len(WorkspaceStore(directory).snapshot()["contacts"]) == 32


@pytest.mark.parametrize("iteration", range(3))
def test_concurrent_duplicate_phone_has_one_winner(tmp_path, iteration):
    directory = str(tmp_path / "workspace")

    def save(index):
        record = contact(index, phone="+1 (941) 555-9900")
        try:
            return WorkspaceStore(directory).put_contact(record["id"], record)["id"]
        except WorkspaceConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(save, range(1, 9)))
    assert len([result for result in results if result]) == 1
    assert len(WorkspaceStore(directory).snapshot()["contacts"]) == 1


def _process_save_contact(args):
    directory, index = args
    record = contact(index)
    return WorkspaceStore(directory).put_contact(record["id"], record)


def test_multiple_server_processes_share_transactions(tmp_path):
    directory = str(tmp_path / "workspace")
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_process_save_contact, [(directory, index) for index in range(1, 13)]))
    assert len(results) == len(WorkspaceStore(directory).snapshot()["contacts"]) == 12


def test_import_skips_stale_ids_and_normalized_duplicate_phones(tmp_path):
    store = WorkspaceStore(str(tmp_path / "workspace"))
    current = contact(firstName="Current")
    store.put_contact(current["id"], current)
    incoming = payload(contacts=[contact(firstName="Old"), contact(2, phone="+1 (941) 666-0001"), contact(3)])
    expected = store.import_records(incoming)
    assert expected["contacts"] == [{**current, "revision": 1}, {**contact(3), "revision": 1}]
    assert store.import_records(incoming) == expected


def test_legacy_agent_import_has_stable_id_and_deduplicates_explicit_ids(tmp_path):
    store = WorkspaceStore(str(tmp_path / "workspace"))
    legacy = {"name": "Reception", "prompt": "Ask how we can help."}
    result = store.import_records(payload(agents=[legacy]))
    saved = result["agents"][0]
    assert saved["id"].startswith("agent-import-") and saved["createdAt"] == ""
    same = {**legacy, "id": "agent-another-0001", "createdAt": ""}
    assert store.import_records(payload(agents=[legacy, same])) == result
    assert WorkspaceStore(str(tmp_path / "other")).import_records(payload(agents=[legacy])) == result


def test_import_invalid_record_and_capacity_failure_roll_back_every_record(tmp_path):
    store = WorkspaceStore(str(tmp_path / "workspace"))
    with pytest.raises(WorkspaceError):
        store.import_records(payload(contacts=[contact(), contact(2, phone="invalid")]))
    assert store.snapshot()["contacts"] == []
    store.import_records(payload(agents=[agent(i) for i in range(1, 51)]))
    with pytest.raises(WorkspaceConflict):
        store.import_records(payload(contacts=[contact()], agents=[agent(51)]))
    assert store.snapshot()["contacts"] == []
    assert len(store.snapshot()["agents"]) == 50


def test_contact_capacity_allows_updates_but_rejects_new_record(tmp_path):
    store = WorkspaceStore(str(tmp_path / "workspace"))
    store.import_records(payload(contacts=[contact(i) for i in range(1, 501)]))
    updated = contact(firstName="Updated", revision=1)
    assert store.put_contact(updated["id"], updated) == {**updated, "revision": 2}
    with pytest.raises(WorkspaceConflict):
        store.put_contact(contact(501)["id"], contact(501))


def test_demo_overrides_remain_separate_and_reserve_unedited_demo_phone(tmp_path):
    store = WorkspaceStore(str(tmp_path / "workspace"))
    ident, phone = next(iter(DEMO_PHONES.items()))
    with pytest.raises(WorkspaceConflict):
        store.put_contact(contact()["id"], contact(phone=phone))
    demo = contact(id=ident, phone="+19415559999", note="Legacy demo note", demo=False)
    saved = store.put_contact(ident, demo)
    assert saved["demo"] is True and "note" not in saved
    assert store.snapshot()["demoOverrides"] == [saved]
    # Once the real override changes its phone, the old number is available.
    store.put_contact(contact()["id"], contact(phone=phone))
    assert len(store.snapshot()["contacts"]) == 1


@pytest.mark.parametrize("changes", [
    {"id": "local-short"}, {"firstName": ""}, {"lastName": "x" * 81}, {"phone": "9415551234"},
    {"phone": "tel:+19415551234"}, {"phone": "+012345678"}, {"website": "javascript:alert(1)"},
    {"website": "https://example.com:bad"}, {"website": "https://user:pass@example.com"},
    {"website": "https://example.com/has space"}, {"email": 123}, {"labels": ["a"] * 11},
    {"labels": [""]}, {"createdAt": "2026-02-30"}, {"status": []}, {"extra": "ignored?"},
    {"firstName": "Hello\x00World"}, {"lastName": "\ud800"},
])
def test_invalid_contacts_are_rejected_without_saving(tmp_path, changes):
    store = WorkspaceStore(str(tmp_path / "workspace"))
    value = contact(**changes)
    with pytest.raises(WorkspaceError):
        store.put_contact(value["id"], value)
    assert store.snapshot()["contacts"] == []


@pytest.mark.parametrize("field,count", [("contacts", 501), ("demoOverrides", 5), ("agents", 51)])
def test_import_array_bounds(tmp_path, field, count):
    with pytest.raises(WorkspaceError):
        WorkspaceStore(str(tmp_path / "workspace")).import_records(payload(**{field: [{}] * count}))


@pytest.mark.parametrize("path", ["", "relative/path", "/", "/private/../tmp/workspace"])
def test_disabled_or_unsafe_paths_have_no_fallback(path):
    with pytest.raises(WorkspaceUnavailable):
        WorkspaceStore(path).snapshot()


@pytest.mark.parametrize("kind", ["directory-symlink", "database-symlink", "database-hardlink", "database-fifo", "journal-symlink"])
def test_storage_rejects_indirection_and_nonregular_files(tmp_path, kind):
    directory, other = tmp_path / "workspace", tmp_path / "other"
    other.mkdir()
    directory.mkdir()
    target = other / "private.txt"
    target.write_text("must not change")
    if kind == "directory-symlink":
        directory.rmdir()
        directory.symlink_to(other, target_is_directory=True)
    elif kind == "database-symlink":
        (directory / DATABASE_NAME).symlink_to(target)
    elif kind == "database-hardlink":
        os.link(target, directory / DATABASE_NAME)
    elif kind == "database-fifo":
        os.mkfifo(directory / DATABASE_NAME)
    else:
        (directory / (DATABASE_NAME + "-journal")).symlink_to(target)
    with pytest.raises(WorkspaceUnavailable):
        WorkspaceStore(str(directory)).snapshot()
    assert target.read_text() == "must not change"


@pytest.mark.parametrize("corruption", ["invalid-file", "newer-version", "invalid-record"])
def test_corrupt_or_unsupported_storage_reports_unavailable(tmp_path, corruption):
    directory = tmp_path / "workspace"
    store = WorkspaceStore(str(directory))
    store.put_contact(contact()["id"], contact())
    if corruption == "invalid-file":
        (directory / DATABASE_NAME).write_bytes(b"not sqlite")
    else:
        with sqlite3.connect(directory / DATABASE_NAME) as database:
            if corruption == "newer-version":
                database.execute("PRAGMA user_version=999")
            else:
                database.execute("UPDATE contacts SET payload=?", (json.dumps(contact(firstName=None)),))
    with pytest.raises(WorkspaceUnavailable):
        store.snapshot()


def test_disk_failure_is_sanitized_and_never_returns_a_saved_record(tmp_path, monkeypatch):
    store = WorkspaceStore(str(tmp_path / "workspace"))

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("private filesystem details")

    monkeypatch.setattr(sqlite3, "connect", fail)
    with pytest.raises(WorkspaceUnavailable, match="Changes were not saved") as error:
        store.put_contact(contact()["id"], contact())
    assert "private filesystem" not in str(error.value)


def test_settings_workspace_path_is_explicit_absolute_and_read_from_environment(tmp_path, monkeypatch):
    settings = Settings("AC" + "1" * 32, "test-token", "https://operator.example")
    assert settings.workspace_storage_dir == ""
    for invalid in ("relative", "/", "/private/../tmp/shared"):
        with pytest.raises(ValueError, match="WORKSPACE_STORAGE_DIR"):
            replace(settings, workspace_storage_dir=invalid)
    directory = str(tmp_path / "persistent")
    assert replace(settings, workspace_storage_dir=directory).workspace_storage_dir == directory
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", settings.account_sid)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", settings.auth_token)
    monkeypatch.setenv("PUBLIC_BASE_URL", settings.public_base_url)
    monkeypatch.setenv("WORKSPACE_STORAGE_DIR", directory)
    monkeypatch.setattr("config.load_dotenv", lambda *args, **kwargs: None)
    assert Settings.from_env().workspace_storage_dir == directory


def test_stale_contact_and_draft_edits_are_rejected_atomically(tmp_path):
    directory = str(tmp_path / "workspace")
    first, second = WorkspaceStore(directory), WorkspaceStore(directory)
    for kind, value in (("contact", contact()), ("agent", agent())):
        save = getattr(first, "put_" + kind)
        saved = save(value["id"], value)
        stale = dict(saved)
        field = "firstName" if kind == "contact" else "prompt"
        changed = getattr(second, "put_" + kind)(value["id"], {**saved, field: "Newer edit"})
        assert changed["revision"] == 2
        for invalid_revision in (None, 0, 1):
            payload = {**stale, field: "Stale edit"}
            if invalid_revision is None:
                payload.pop("revision")
            else:
                payload["revision"] = invalid_revision
            with pytest.raises(WorkspaceConflict, match="changed in another browser"):
                save(value["id"], payload)
        assert first.snapshot()["contacts" if kind == "contact" else "agents"] == [changed]
        latest = save(value["id"], {**changed, field: "Reviewed latest"})
        assert latest["revision"] == 3


def test_concurrent_updates_to_same_revision_have_one_winner(tmp_path):
    directory = str(tmp_path / "workspace")
    saved = WorkspaceStore(directory).put_contact(contact()["id"], contact())

    def edit(number):
        try:
            return WorkspaceStore(directory).put_contact(saved["id"], {**saved, "firstName": str(number)})
        except WorkspaceConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(edit, range(8)))
    winners = [value for value in results if value]
    assert len(winners) == 1 and winners[0]["revision"] == 2


def test_legacy_payload_without_revision_remains_editable_after_upgrade(tmp_path):
    directory = tmp_path / "workspace"
    store = WorkspaceStore(str(directory))
    store.put_contact(contact()["id"], contact())
    with sqlite3.connect(directory / DATABASE_NAME) as database:
        database.execute("UPDATE contacts SET payload=?", (json.dumps(contact()),))
    old = store.snapshot()["contacts"][0]
    assert old["revision"] == 1
    assert store.put_contact(old["id"], {**old, "firstName": "Updated"})["revision"] == 2


@pytest.mark.parametrize("revision", [True, -1, "1", 1.5, 2**53])
def test_invalid_revisions_cannot_write(tmp_path, revision):
    store = WorkspaceStore(str(tmp_path / "workspace"))
    with pytest.raises(WorkspaceError):
        store.put_contact(contact()["id"], contact(revision=revision))
    with pytest.raises(WorkspaceError):
        store.put_agent(agent()["id"], agent(revision=revision))
    assert store.snapshot()["contacts"] == store.snapshot()["agents"] == []


def test_corrupt_saved_revision_fails_closed_without_replacing_record(tmp_path):
    directory = tmp_path / "workspace"
    store = WorkspaceStore(str(directory))
    old = store.put_contact(contact()["id"], contact())
    with sqlite3.connect(directory / DATABASE_NAME) as database:
        database.execute("UPDATE contacts SET payload=?", (json.dumps({**old, "revision": "invalid"}),))
    with pytest.raises(WorkspaceUnavailable):
        store.put_contact(old["id"], {**old, "firstName": "Should not be saved"})
    with sqlite3.connect(directory / DATABASE_NAME) as database:
        record = json.loads(database.execute("SELECT payload FROM contacts").fetchone()[0])
    assert record["firstName"] == old["firstName"] and record["revision"] == "invalid"
