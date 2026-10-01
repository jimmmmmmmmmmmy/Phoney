"""Shared fixtures and fakes for focused integration checks."""

from contextlib import contextmanager


import os


import uuid


import pytest


from agent_registry import AgentRegistry, RegistryError


from notification_store import NotificationStore


import postgres_store


from postgres_store import PostgresUnavailable, schema_name


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
