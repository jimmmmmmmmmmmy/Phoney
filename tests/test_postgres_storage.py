"""Opt-in integration tests against an explicitly supplied disposable database.

Set PHONEY_TEST_DATABASE_URL to run PostgreSQL cases. Each case creates and
removes random workspace schemas; never point this variable at production.
"""

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
import os
import multiprocessing
import sqlite3
import uuid

import pytest

from agent_registry import AgentRegistry, RegistryError
from notification_store import NotificationStore
import postgres_store
from postgres_store import PostgresUnavailable, register_workspace, schema_name, transaction
from scripts.migrate_postgres import MigrationError, main as migration_main, migrate, read_source
from workspace_store import WorkspaceConflict, WorkspaceStore, WorkspaceUnavailable


AGENT_ID = "agent-test-00000001"
VOICE_ID = "voiceABC123456789"
SID = "CA" + "1" * 32


def contact(index=1, **changes):
    return {"id": f"local-{index:08d}", "firstName": "Test", "lastName": str(index),
            "phone": f"+1941555{index:04d}", "createdAt": "2026-09-30T12:00:00Z", **changes}


def draft(**changes):
    return {"id": AGENT_ID, "name": "Test draft", "prompt": "Ask how we can help.",
            "createdAt": "2026-09-30T12:00:00Z", **changes}


def voice(**changes):
    return {"name": "Owner", "voiceId": VOICE_ID, "ready": True,
            "requiresVerification": False, **changes}


def agent(**changes):
    return {"name": "Reception", "prompt": "Ask how we can help.",
            "voiceProfileId": "voice-" + VOICE_ID, "slot": 1, **changes}


@pytest.fixture
def pg_workspaces():
    database_url = os.getenv("PHONEY_TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("Set PHONEY_TEST_DATABASE_URL to a disposable PostgreSQL database.")
    psycopg = pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")
    created = []

    def workspace():
        ident = "test-" + uuid.uuid4().hex
        created.append(ident)
        return database_url, ident

    yield workspace
    postgres_store.close_pools()
    with psycopg.connect(database_url) as connection:
        for ident in created:
            connection.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                psycopg.sql.Identifier(schema_name(ident))))
            if connection.execute("SELECT to_regclass('phoney_catalog.workspaces')").fetchone()[0]:
                connection.execute("DELETE FROM phoney_catalog.workspace_phone_numbers WHERE workspace_id=%s", (ident,))
                connection.execute("DELETE FROM phoney_catalog.workspaces WHERE id=%s", (ident,))


def test_schema_names_are_stable_and_cannot_inject_identifiers():
    assert schema_name("default") == schema_name("default")
    assert schema_name("default") != schema_name("other")
    assert len(schema_name('a"; DROP SCHEMA public;--')) <= 63
    assert schema_name('a"; DROP SCHEMA public;--').replace("_", "").isalnum()
    with pytest.raises(ValueError):
        schema_name("")


def test_postgres_failures_never_fall_back_to_sqlite_or_expose_connection_details(tmp_path, monkeypatch):
    @contextmanager
    def unavailable(*args, **kwargs):
        raise PostgresUnavailable("password=private-test-value")
        yield

    monkeypatch.setattr("workspace_store.postgres_transaction", unavailable)
    monkeypatch.setattr("agent_registry.store.postgres_transaction", unavailable)
    folder = tmp_path / "must-not-be-created"
    with pytest.raises(WorkspaceUnavailable) as workspace_error:
        WorkspaceStore(str(folder), database_url="configured").snapshot()
    with pytest.raises(RegistryError) as registry_error:
        AgentRegistry(str(folder), database_url="configured").snapshot()
    assert "private-test-value" not in str(workspace_error.value)
    assert "private-test-value" not in str(registry_error.value)
    assert not folder.exists()


def test_postgres_round_trip_order_revisions_import_and_restart(pg_workspaces):
    url, ident = pg_workspaces()
    store = WorkspaceStore("", database_url=url, workspace_id=ident)
    third = store.put_contact(contact(3)["id"], contact(3))
    first = store.put_contact(contact()["id"], contact(firstName="Zoë 刘"))
    saved_draft = store.put_agent(AGENT_ID, draft())
    updated = store.put_contact(third["id"], {**third, "firstName": "Updated"})
    imported = store.import_records({"contacts": [contact(3), contact(2, phone=first["phone"]), contact(4)],
                                    "demoOverrides": [], "agents": [draft(prompt="Stale")]})
    assert imported["contacts"][:2] == [updated, first]
    assert [row["id"] for row in imported["contacts"]] == [third["id"], first["id"], contact(4)["id"]]
    assert imported["agents"] == [saved_draft]
    assert WorkspaceStore("", database_url=url, workspace_id=ident).snapshot() == imported
    with pytest.raises(WorkspaceConflict):
        store.put_contact(third["id"], third)


def test_postgres_workspaces_isolate_contacts_agents_notifications_and_sessions(pg_workspaces):
    url, first_id = pg_workspaces()
    _, second_id = pg_workspaces()
    first = WorkspaceStore("", database_url=url, workspace_id=first_id)
    second = WorkspaceStore("", database_url=url, workspace_id=second_id)
    first.put_contact(contact()["id"], contact(firstName="One"))
    second.put_contact(contact()["id"], contact(firstName="Two"))
    assert first.snapshot()["contacts"][0]["firstName"] == "One"
    assert second.snapshot()["contacts"][0]["firstName"] == "Two"
    registry = AgentRegistry("", database_url=url, workspace_id=first_id)
    other = AgentRegistry("", database_url=url, workspace_id=second_id)
    registry.add_voice(voice())
    registry.publish(AGENT_ID, agent())
    token = registry.consume_grant(registry.grant())
    assert registry.authorized(token) and not other.authorized(token)
    assert other.snapshot() == {"agents": [], "voices": []}
    other.add_voice(voice())
    assert other.publish(AGENT_ID, agent(name="Second")).slot == 1
    NotificationStore(first).observe([{"call_sid": SID, "active": True}])
    assert NotificationStore(second).observe([]) == {"notifications": []}


def test_postgres_concurrent_duplicate_phone_and_stale_edit_have_one_winner(pg_workspaces):
    url, ident = pg_workspaces()

    def duplicate(index):
        store = WorkspaceStore("", database_url=url, workspace_id=ident)
        record = contact(index, phone="+19415559999")
        try:
            return store.put_contact(record["id"], record)
        except WorkspaceConflict:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        records = [row for row in pool.map(duplicate, range(1, 7)) if row]
    assert len(records) == 1
    original = records[0]

    def edit(index):
        try:
            return WorkspaceStore("", database_url=url, workspace_id=ident).put_contact(
                original["id"], {**original, "firstName": f"Edit {index}"})
        except WorkspaceConflict:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        edits = [row for row in pool.map(edit, range(6)) if row]
    assert len(edits) == 1 and edits[0]["revision"] == 2
    assert WorkspaceStore("", database_url=url, workspace_id=ident).snapshot()["contacts"] == edits


def _process_save_postgres_contact(arguments):
    url, ident, index = arguments
    record = contact(index)
    return WorkspaceStore("", database_url=url, workspace_id=ident).put_contact(record["id"], record)


def test_postgres_transactions_are_shared_across_server_processes(pg_workspaces):
    url, ident = pg_workspaces()
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        saved = list(pool.map(_process_save_postgres_contact, [(url, ident, index) for index in range(1, 7)]))
    records = WorkspaceStore("", database_url=url, workspace_id=ident).snapshot()["contacts"]
    assert {row["id"] for row in records} == {row["id"] for row in saved}
    assert len(records) == 6


def test_postgres_import_failure_rolls_back_all_records(pg_workspaces, monkeypatch):
    url, ident = pg_workspaces()
    store = WorkspaceStore("", database_url=url, workspace_id=ident)
    original = store.put_contact(contact()["id"], contact())
    monkeypatch.setattr("workspace_store.MAX_CONTACTS", 2)
    with pytest.raises(WorkspaceConflict):
        store.import_records({"contacts": [contact(2), contact(3)], "demoOverrides": [], "agents": []})
    assert store.snapshot()["contacts"] == [original]


def test_postgres_notifications_keep_read_state_on_restart(pg_workspaces):
    url, ident = pg_workspaces()
    first = NotificationStore(WorkspaceStore("", database_url=url, workspace_id=ident))
    notification = first.observe([{"call_sid": SID, "active": True}])["notifications"][0]
    assert notification["unread"]
    first.mark_read({"ids": [SID]})
    restarted = NotificationStore(WorkspaceStore("", database_url=url, workspace_id=ident))
    assert not restarted.observe([{"call_sid": SID, "active": False}])["notifications"][0]["unread"]


def test_postgres_publication_and_grants_are_atomic(pg_workspaces):
    url, ident = pg_workspaces()
    store = AgentRegistry("", database_url=url, workspace_id=ident)
    store.add_voice(voice(), consent=True)
    store.add_voice(voice(name="Changed"), consent=True)
    original = store.publish(AGENT_ID, agent(expectedRevision=0), require_revision=True)

    def publish(index):
        try:
            return AgentRegistry("", database_url=url, workspace_id=ident).publish(
                AGENT_ID, agent(name=f"Edit {index}", expectedRevision=1), require_revision=True)
        except RegistryError as error:
            assert error.status_code == 409
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        published = [row for row in pool.map(publish, range(6)) if row]
    assert len(published) == 1 and published[0].revision == 2
    assert original.revision == 1 and original.name == "Reception"
    with store._transaction() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM clone_receipts").fetchone()[0] == 1
    grant = store.grant()

    def consume(_):
        try:
            return store.consume_grant(grant)
        except RegistryError as error:
            assert error.status_code == 403
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        tokens = [value for value in pool.map(consume, range(6)) if value]
    assert len(tokens) == 1 and store.authorized(tokens[0])
    store.revoke(tokens[0])
    assert not store.authorized(tokens[0])


def test_postgres_owner_session_retains_subsecond_epoch_expiry(pg_workspaces):
    url, ident = pg_workspaces()
    now = [1_700_000_000.125]
    store = AgentRegistry("", database_url=url, workspace_id=ident, clock=lambda: now[0])
    token = store.consume_grant(store.grant())
    now[0] += 12 * 60 * 60 - 0.01
    assert store.authorized(token)
    now[0] += 0.02
    assert not store.authorized(token)


def test_workspace_catalog_phone_mapping_cannot_reassign_another_workspace(pg_workspaces):
    url, first = pg_workspaces()
    _, second = pg_workspaces()
    # Randomized synthetic number keeps parallel test runs independent.
    phone = "+19" + str(int(uuid.uuid4().hex[:10], 16)).zfill(12)
    record = register_workspace(url, first, phone)
    assert record["schema"] == schema_name(first)
    assert register_workspace(url, first, phone) == record
    with pytest.raises(PostgresUnavailable):
        register_workspace(url, second, phone)
    with transaction(url, first) as db:
        assert db.execute("SELECT workspace_id FROM phoney_catalog.workspace_phone_numbers WHERE phone_number=?",
                          (phone,)).fetchone() == (first,)


def make_sqlite_source(tmp_path):
    folder = tmp_path / "sqlite-source"
    workspace = WorkspaceStore(str(folder))
    workspace.put_contact(contact(3)["id"], contact(3))
    first = workspace.put_contact(contact()["id"], contact())
    workspace.put_contact(first["id"], {**first, "firstName": "Updated"})
    workspace.put_agent(AGENT_ID, draft())
    inbox = NotificationStore(workspace)
    inbox.observe([{"call_sid": SID, "active": True}])
    inbox.mark_read({"ids": [SID]})
    registry = AgentRegistry(str(folder))
    registry.add_voice(voice(name="owner"), consent=True)
    registry.ensure_default_voice_clone()
    registry.publish(AGENT_ID, agent(slot=2))
    registry.publish(AGENT_ID, agent(slot=2, prompt="Second immutable revision"))
    token = registry.consume_grant(registry.grant())
    registry.grant()
    return folder, workspace, registry, token


def test_sqlite_source_inspection_is_read_only_and_does_not_emit_payloads(tmp_path):
    folder, _, _, _ = make_sqlite_source(tmp_path)
    before = {file.name: file.read_bytes() for file in folder.iterdir()}
    snapshot = read_source(folder)
    assert set(snapshot) == set(postgres_store.TABLES)
    assert all(snapshot[table]["rows"] for table in postgres_store.TABLES)
    assert {file.name: file.read_bytes() for file in folder.iterdir()} == before
    assert snapshot["contacts"]["columns"][-1] == "insertion_order"


def test_migration_dry_run_creates_no_schema_apply_preserves_all_tables_and_order(tmp_path, pg_workspaces):
    url, ident = pg_workspaces()
    folder, workspace, registry, token = make_sqlite_source(tmp_path)
    expected_workspace = workspace.snapshot()
    expected_registry = registry.snapshot()
    expected_notifications = NotificationStore(workspace).observe([{"call_sid": SID, "active": True}])
    before = {file.name: file.read_bytes() for file in folder.iterdir()}
    report = migrate(folder, url, ident)
    assert report["mode"] == "dry-run" and not report["target_schema_exists"]
    with transaction(url, ident, initialize=False, read_only=True) as db:
        assert db.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=?)",
                          (schema_name(ident),)).fetchone() == (False,)
    copied = migrate(folder, url, ident, apply=True)
    assert copied["verified_counts"] == report["source_counts"]
    assert {file.name: file.read_bytes() for file in folder.iterdir()} == before
    target = WorkspaceStore("", database_url=url, workspace_id=ident)
    assert target.snapshot() == expected_workspace
    target_registry = AgentRegistry("", database_url=url, workspace_id=ident)
    assert target_registry.snapshot() == expected_registry
    assert target_registry.authorized(token)
    assert NotificationStore(target).observe([{"call_sid": SID, "active": True}]) == expected_notifications
    added = target.put_contact(contact(4)["id"], contact(4))
    assert target.snapshot()["contacts"][-1] == added
    with pytest.raises(MigrationError, match="already contains"):
        migrate(folder, url, ident, apply=True)


def test_pg_transaction_rolls_back_and_resets_schema_for_pool_reuse(pg_workspaces):
    url, first = pg_workspaces()
    _, second = pg_workspaces()
    with pytest.raises(RuntimeError):
        with transaction(url, first) as db:
            db.execute("INSERT INTO agent_setup VALUES (?,?)", ("test", "must roll back"))
            raise RuntimeError("abort synthetic transaction")
    with transaction(url, second) as db:
        assert db.execute("SELECT value FROM agent_setup").fetchall() == []
    with transaction(url, first) as db:
        assert db.execute("SELECT value FROM agent_setup").fetchall() == []
    with pytest.raises(PostgresUnavailable):
        with transaction(url, first, initialize=False, read_only=True) as db:
            db.execute("INSERT INTO agent_setup VALUES (?,?)", ("test", "readonly"))


def test_migration_cli_reads_explicit_environment_file_and_defaults_to_dry_run(tmp_path, pg_workspaces, capsys):
    url, ident = pg_workspaces()
    folder, _, _, _ = make_sqlite_source(tmp_path)
    env_file = tmp_path / "migration.env"
    env_file.write_text(f"DATABASE_URL={url}\nWORKSPACE_STORAGE_DIR={folder}\nWORKSPACE_ID={ident}\n")
    assert migration_main(["--env-file", str(env_file)]) == 0
    output = capsys.readouterr().out
    assert '"mode": "dry-run"' in output and ident in output
    assert url not in output and "Second immutable revision" not in output


def test_migration_unsupported_target_version_is_rejected_without_changes(tmp_path, pg_workspaces):
    url, ident = pg_workspaces()
    folder, _, _, _ = make_sqlite_source(tmp_path)
    with transaction(url, ident) as db:
        db.execute("UPDATE phoney_storage_meta SET version=999 WHERE id=1")
    with pytest.raises(MigrationError, match="version is unsupported"):
        migrate(folder, url, ident)
    with transaction(url, ident, initialize=False, read_only=True) as db:
        assert db.execute("SELECT count(*) FROM contacts").fetchone() == (0,)
        assert db.execute("SELECT version FROM phoney_storage_meta WHERE id=1").fetchone() == (999,)
