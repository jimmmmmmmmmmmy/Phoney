"""Workspace access configuration must fail closed and keep connection secrets private."""

from dataclasses import replace

import pytest

from config import Settings


BASE = dict(account_sid="AC" + "1" * 32, auth_token="test-auth",
            public_base_url="https://operator.example")


@pytest.mark.parametrize("url", [
    "sqlite:///local.db", "postgresql://db.example/phoney", "postgresql://localhost/",
    "postgresql:///phoney?host=db.example", "postgresql:///phoney?hostaddr=8.8.8.8",
    "postgresql:///phoney?service=remote", "postgresql://localhost:invalid/phoney",
])
def test_database_must_be_local_postgres(url):
    with pytest.raises(ValueError, match="local PostgreSQL"):
        Settings(**BASE, database_url=url)


@pytest.mark.parametrize("url", [
    "postgresql://localhost/phoney", "postgresql://127.0.0.1:5432/phoney",
    "postgresql://[::1]/phoney", "postgresql:///phoney?host=/tmp/phoney-postgres",
])
def test_local_tcp_and_unix_socket_databases_are_supported(url):
    configured = Settings(**BASE, database_url=url, workspace_access_enabled=True,
                          agent_management_enabled=True)
    assert configured.database_url == url


def test_access_cannot_be_enabled_without_durable_storage():
    with pytest.raises(ValueError, match="before enabling workspace access"):
        Settings(**BASE, workspace_access_enabled=True)


@pytest.mark.parametrize("workspace", ["", "../other", "MixedCase", "a" * 65])
def test_workspace_id_is_a_stable_slug(workspace):
    with pytest.raises(ValueError, match="WORKSPACE_ID"):
        Settings(**BASE, workspace_id=workspace)


def test_database_credentials_do_not_appear_in_settings_repr():
    database = "postgresql://phoney:private-database-password@localhost/phoney"
    configured = Settings(**BASE, database_url=database)
    assert database not in repr(configured)
    assert "private-database-password" not in repr(configured)


def test_invalid_access_toggle_does_not_silently_disable_protection():
    with pytest.raises(ValueError, match="WORKSPACE_ACCESS_ENABLED"):
        replace(Settings(**BASE), workspace_access_enabled="false")
