"""Shared fixtures and fakes for focused integration checks."""

from dataclasses import replace


import os


import uuid


from fastapi.testclient import TestClient


import pytest


from app import create_app


from config import Settings


BASE = Settings("AC" + "1" * 32, "offline-test-token", "https://operator.example")


PIN = "142857"


PASSWORD = "local test recovery password"


@pytest.fixture(params=["sqlite", "postgres"])
def configured_app(tmp_path, request):
    database_url = ""
    if request.param == "postgres":
        database_url = os.getenv("PHONEY_TEST_DATABASE_URL", "")
        if not database_url:
            pytest.skip("An isolated PHONEY_TEST_DATABASE_URL is needed")
    settings = replace(BASE, workspace_storage_dir=str(tmp_path / "workspace"),
                       database_url=database_url, workspace_id="test_" + uuid.uuid4().hex,
                       workspace_access_enabled=True, agent_management_enabled=True)
    app = create_app(settings)
    app.state.workspace_access.configure(PIN, PASSWORD)
    return app, settings
