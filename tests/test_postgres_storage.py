"""Focused product and boundary checks; test helpers live in support."""

from contextlib import contextmanager

import pytest

from agent_registry import AgentRegistry, RegistryError
from notification_store import NotificationStore
from postgres_store import PostgresUnavailable
from workspace_store import WorkspaceConflict, WorkspaceStore, WorkspaceUnavailable

from support.postgres_storage import AGENT_ID, SID, agent, contact, draft, pg_workspaces, voice


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
