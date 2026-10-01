"""Shared fixtures and fakes for focused integration checks."""

from concurrent.futures import ThreadPoolExecutor


import hashlib


import sqlite3


from types import SimpleNamespace


from fastapi import FastAPI, Request, WebSocket


from fastapi.testclient import TestClient


import pytest


from twilio.request_validator import RequestValidator


from workspace_auth import SESSION_SECONDS, WorkspaceAccess, install_workspace_access


from workspace_auth.web import SESSION_COOKIE


PIN = "642815"


PASSWORD = "test recovery phrase 924"


ORIGIN = "https://phoney.example"


HEADERS = {"Origin": ORIGIN, "X-Workspace-Access": "1"}


@pytest.fixture
def access(tmp_path):
    service = WorkspaceAccess(str(tmp_path.resolve()))
    service.configure(PIN, PASSWORD)
    return service


def unlock(service, device=None, ip="192.0.2.1", method="pin", value=PIN, remember=True):
    return service.unlock(device, ip, method, value, remember)


def app_client(tmp_path, *, enabled=True, configured=True):
    settings = SimpleNamespace(workspace_access_enabled=enabled, database_url="",
                               workspace_id="default", workspace_storage_dir=str(tmp_path.resolve()),
                               public_base_url=ORIGIN, account_sid="AC" + "a" * 32,
                               auth_token="test-token")
    app = FastAPI()
    access = install_workspace_access(app, settings)
    if configured and access:
        access.configure(PIN, PASSWORD)

    @app.get("/dashboard")
    @app.get("/api/recordings/secret/audio")
    @app.get("/api/transcripts/secret/export")
    @app.get("/api/workspace")
    @app.get("/future-private-route")
    async def private(request: Request):
        return {"secret": "workspace-content", "authenticated":
                getattr(request.state, "workspace_authenticated", False)}

    @app.post("/voice")
    async def voice(request: Request):
        return dict(await request.form())

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.websocket("/media/{call_sid}/")
    async def media(websocket: WebSocket, call_sid: str):
        await websocket.accept()
        await websocket.send_text("accepted")
        await websocket.close()

    return TestClient(app, base_url=ORIGIN), access, settings
