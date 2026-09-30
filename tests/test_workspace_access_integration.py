"""The real app must protect private routes while retaining signed phone callbacks."""

from dataclasses import replace
import os
import time
import uuid

from fastapi.testclient import TestClient
import pytest
from twilio.request_validator import RequestValidator

from app import create_app
from config import Settings
from workspace_auth import SESSION_SECONDS
from workspace_auth.web import SESSION_COOKIE


BASE = Settings("AC" + "1" * 32, "offline-test-token", "https://operator.example")
PIN = "142857"
PASSWORD = "local test recovery password"
HEADERS = {"Origin": BASE.public_base_url, "X-Workspace-Access": "1"}


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


def unlock(client, credential=PIN, method="pin"):
    return client.post("/api/workspace-access/unlock",
                       json={"method": method, "credential": credential, "remember": True},
                       headers=HEADERS)


def test_private_pages_apis_downloads_and_unknown_routes_never_return_anonymous_data(configured_app):
    app, _ = configured_app
    sid = "CA" + "a" * 32
    paths = ["/dashboard", "/team", "/docs", "/openapi.json", "/api/workspace",
             "/api/notifications", "/api/transcripts", f"/api/transcripts/{sid}/export?format=json",
             f"/api/recordings/{sid}/audio", f"/api/voicemails/{sid}/audio",
             "/resumes/james-liu.pdf", "/assets/dashboard-workspace.js", "/api/future-private-route"]
    with TestClient(app, base_url=BASE.public_base_url) as client:
        for path in paths:
            response = client.get(path, follow_redirects=False, headers={"Range": "bytes=0-100"})
            assert response.status_code in {401, 303}, path
            assert "noindex" in response.headers["x-robots-tag"]
            assert response.headers["cache-control"] == "no-store"
            assert "<!doctype html>" not in response.text.lower()
        assert client.head(f"/api/voicemails/{sid}/audio").status_code == 401
        assert client.get("/unlock").status_code == 200
        assert client.get("/assets/workspace-unlock.js").status_code == 200


def test_three_pin_failures_survive_cookie_reset_and_password_restores_access(configured_app):
    app, _ = configured_app
    with TestClient(app, base_url=BASE.public_base_url) as client:
        for remaining in (2, 1):
            response = unlock(client, "000000")
            assert response.status_code == 401
            assert response.json()["attempts_remaining"] == remaining
        assert unlock(client, "000000").json()["error"] == "password_required"
        assert client.get("/api/workspace-access/status").json()["password_required"] is True
        client.cookies.clear()
        assert client.get("/api/workspace-access/status").json()["password_required"] is True
        assert unlock(client).status_code == 403
        assert unlock(client, PASSWORD, "password").status_code == 200
        assert client.get("/api/workspace").status_code == 200


def test_remembered_device_survives_app_restart_and_expires_after_thirty_days(configured_app):
    app, settings = configured_app
    with TestClient(app, base_url=BASE.public_base_url) as client:
        response = unlock(client)
        assert response.status_code == 200
        cookies = response.headers.get_list("set-cookie")
        session = next(value for value in cookies if value.startswith(SESSION_COOKIE + "="))
        assert f"Max-Age={SESSION_SECONDS}" in session
        assert all(flag in session for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"))
        token = client.cookies.get(SESSION_COOKIE)
    restarted = create_app(settings)
    with TestClient(restarted, base_url=BASE.public_base_url) as client:
        client.cookies.set(SESSION_COOKIE, token)
        assert client.get("/api/workspace-access/status").json()["authenticated"] is True
        assert client.get("/dashboard").status_code == 200
        restarted.state.workspace_access.clock = lambda: time.time() + SESSION_SECONDS + 1
        assert client.get("/api/workspace").status_code == 401


def test_lock_revokes_the_server_session_even_if_old_cookie_is_replayed(configured_app):
    app, _ = configured_app
    with TestClient(app, base_url=BASE.public_base_url) as client:
        assert unlock(client).status_code == 200
        token = client.cookies.get(SESSION_COOKIE)
        assert client.post("/api/workspace-access/logout", json={}, headers=HEADERS).status_code == 200
        client.cookies.set(SESSION_COOKIE, token)
        assert client.get("/api/workspace").status_code == 401


def test_shared_unlock_also_grants_existing_owner_controls(configured_app):
    app, _ = configured_app
    with TestClient(app, base_url=BASE.public_base_url) as client:
        assert client.get("/api/agents/config").status_code == 401
        assert unlock(client).status_code == 200
        assert client.get("/api/agents/config").json()["authenticated"] is True


def test_phone_callbacks_keep_signature_checks_and_do_not_need_pin(configured_app):
    app, settings = configured_app
    data = {"AccountSid": settings.account_sid, "CallSid": "CA" + "a" * 32}
    signature = RequestValidator(settings.auth_token).compute_signature(settings.public_base_url + "/voice", data)
    with TestClient(app, base_url=BASE.public_base_url) as client:
        assert client.post("/voice", data=data).status_code == 403
        response = client.post("/voice", data=data, headers={"X-Twilio-Signature": signature})
        assert response.status_code == 200
        assert "<Response>" in response.text
        assert client.get("/health").status_code == 200
        assert client.get("/internal/deploy").status_code == 503


def test_unconfigured_access_fails_closed(configured_app):
    _, settings = configured_app
    # A separate workspace has no PIN; prior workspace credentials cannot unlock it.
    settings = replace(settings, workspace_id="test_" + uuid.uuid4().hex,
                       workspace_storage_dir=settings.workspace_storage_dir + "_other")
    app = create_app(settings)
    with TestClient(app, base_url=BASE.public_base_url) as client:
        response = unlock(client)
        assert response.status_code == 503 and response.json()["error"] == "unconfigured"
        assert client.get("/api/workspace").status_code in {401, 503}
