"""Security behavior of the shared PIN screen and durable trusted devices."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
import pytest
from twilio.request_validator import RequestValidator

from workspace_auth import (AccessUnavailable, SESSION_SECONDS, SHORT_SESSION_SECONDS,
                            WorkspaceAccess, install_workspace_access)
from workspace_auth.web import DEVICE_COOKIE, SESSION_COOKIE


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


def test_session_survives_new_store_and_expires_exactly_after_30_days(access):
    now = [1000000]
    access.clock = lambda: now[0]
    code, result, token, _ = unlock(access)
    assert code == 200 and result["authenticated"]
    reopened = WorkspaceAccess(str(access.path.parent), clock=lambda: now[0])
    assert reopened.authenticated(token)
    now[0] += SESSION_SECONDS - 1
    assert reopened.authenticated(token)
    now[0] += 1
    assert not reopened.authenticated(token)


def test_only_hashes_of_credentials_and_session_token_are_stored(access):
    _, _, token, _ = unlock(access)
    connection = sqlite3.connect(access.path)
    pin, password = connection.execute("SELECT pin_hash,password_hash FROM workspace_access_credentials").fetchone()
    stored = connection.execute("SELECT token_hash FROM workspace_access_sessions").fetchone()[0]
    connection.close()
    assert PIN not in pin and PASSWORD not in password
    assert pin.startswith("scrypt-v1:") and password.startswith("scrypt-v1:")
    assert stored == hashlib.sha256(token.encode()).hexdigest() and token not in stored


def test_three_failed_pins_require_password_even_after_refresh_cookie_reset_and_restart(access):
    device = None
    for expected in (2, 1, 0):
        code, result, _, device = unlock(access, device, value="wrong")
        assert result["attempts_remaining"] == expected
        assert code == (403 if expected == 0 else 401)
    state, _ = access.status(device, "192.0.2.1")
    assert state["password_required"]
    reopened = WorkspaceAccess(str(access.path.parent))
    assert unlock(reopened, device, value=PIN)[1]["error"] == "password_required"
    assert unlock(reopened, None, value=PIN)[1]["error"] == "password_required"
    # Changing networks still does not reset the same browser's device lock.
    assert unlock(reopened, device, ip="192.0.2.22", value=PIN)[1]["error"] == "password_required"
    assert unlock(reopened, device, method="password", value=PASSWORD)[0] == 200
    assert not reopened.status(device, "192.0.2.1")[0]["password_required"]


def test_parallel_attempts_cannot_exceed_three_pin_checks(access):
    _, device = access.status(None, "192.0.2.1")
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: unlock(access, device, value="wrong"), range(6)))
    assert sum(result[1]["error"] == "pin_invalid" for result in results) == 2
    assert sum(result[1]["error"] == "password_required" for result in results) == 4


def test_ip_lock_expires_but_device_lock_persists(access):
    now = [1000000]
    access.clock = lambda: now[0]
    device = None
    for _ in range(3):
        _, _, _, device = unlock(access, device, value="wrong")
    now[0] += 15 * 60 + 1
    assert unlock(access, device)[1]["error"] == "password_required"
    assert unlock(access, None)[0] == 200


def test_workspace_pin_cooldown_allows_password_and_does_not_revoke_trusted_phone(access):
    now = [1000000]
    access.clock = lambda: now[0]
    _, _, trusted, _ = unlock(access)
    for i in range(15):
        unlock(access, None, ip=f"192.0.2.{i + 20}", value="wrong")
    code, result, _, device = unlock(access, ip="192.0.2.99")
    assert code == 429 and result["error"] == "throttled" and result["throttle_scope"] == "pin"
    assert access.authenticated(trusted)
    assert unlock(access, device, ip="192.0.2.99", method="password", value=PASSWORD)[0] == 200
    now[0] += 15 * 60 + 1
    assert unlock(access, ip="192.0.2.100")[0] == 200


def test_password_attempts_are_throttled(access):
    device = None
    for _ in range(10):
        code, _, _, device = unlock(access, device, method="password", value="bad password")
        assert code == 401
    code, result, token, _ = unlock(access, device, method="password", value=PASSWORD)
    assert code == 429 and token is None and result["retry_after"] > 0


def test_revocation_only_locks_selected_phone_and_reset_locks_all_phones(access):
    _, _, first, _ = unlock(access)
    _, _, second, _ = unlock(access, ip="192.0.2.2")
    access.revoke(first)
    assert not access.authenticated(first) and access.authenticated(second)
    access.configure("285146", "new recovery phrase 743")
    assert not access.authenticated(second)
    assert unlock(access, value=PIN)[0] == 401
    assert unlock(access, value="285146")[0] == 200


def test_short_session_has_12_hour_server_expiry(access):
    now = [1000000]
    access.clock = lambda: now[0]
    _, _, token, _ = unlock(access, remember=False)
    now[0] += SHORT_SESSION_SECONDS
    assert not access.authenticated(token)


def test_active_sessions_are_bounded_and_newest_session_always_survives(access):
    now = [1000000]
    access.clock = lambda: now[0]
    oldest = newest = None
    for i in range(65):
        code, _, token, _ = unlock(access, ip=f"192.0.2.{i + 1}")
        assert code == 200
        oldest = oldest or token
        newest = token
        now[0] += 1
    assert not access.authenticated(oldest) and access.authenticated(newest)
    with access._transaction() as db:
        assert db.execute("SELECT COUNT(*) AS total FROM workspace_access_sessions").fetchone()["total"] == 64


def test_expired_guess_records_are_pruned(access):
    now = [1000000]
    access.clock = lambda: now[0]
    _, _, _, device = unlock(access, value="wrong")
    now[0] += SESSION_SECONDS + 1
    assert access.status(device, "192.0.2.1")[0]["attempts_remaining"] == 3
    with access._transaction() as db:
        assert db.execute("SELECT COUNT(*) AS total FROM workspace_access_guards").fetchone()["total"] == 0


def test_workspace_identifiers_isolate_credentials_and_sessions(tmp_path):
    root = str(tmp_path.resolve())
    first = WorkspaceAccess(root, workspace_id="first")
    first.configure(PIN, PASSWORD)
    second = WorkspaceAccess(root, workspace_id="second")
    second.configure("285146", "other recovery phrase 743")
    _, _, token, _ = unlock(first)
    assert first.authenticated(token) and not second.authenticated(token)
    assert unlock(second, value=PIN)[0] == 401


def test_all_private_paths_and_api_downloads_are_gated_with_noindex(tmp_path):
    client, _, _ = app_client(tmp_path)
    for path in ("/dashboard", "/future-private-route", "/docs", "/openapi.json"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/unlock"
        assert response.headers["x-robots-tag"].startswith("noindex")
    for path in ("/api/workspace", "/api/recordings/secret/audio", "/api/transcripts/secret/export"):
        response = client.get(path)
        assert response.status_code == 401 and "workspace-content" not in response.text
    assert client.get("/health").status_code == 200
    assert "Disallow:\n" in client.get("/robots.txt").text


def test_success_cookie_is_secure_and_locks_server_session(tmp_path):
    client, access, _ = app_client(tmp_path)
    response = client.post("/api/workspace-access/unlock", headers=HEADERS,
                           json={"method": "pin", "credential": PIN, "remember": True})
    assert response.status_code == 200 and response.json()["redirect"] == "/dashboard"
    cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith(SESSION_COOKIE))
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert f"Max-Age={SESSION_SECONDS}" in cookie and "Domain=" not in cookie
    token = client.cookies.get(SESSION_COOKIE)
    assert access.authenticated(token)
    assert client.get("/api/workspace").json()["authenticated"]
    response = client.post("/api/workspace-access/logout", headers=HEADERS, json={})
    assert response.status_code == 200 and not access.authenticated(token)
    assert client.get("/api/workspace").status_code == 401


def test_unremembered_cookie_is_a_browser_session_cookie(tmp_path):
    client, _, _ = app_client(tmp_path)
    response = client.post("/api/workspace-access/unlock", headers=HEADERS,
                           json={"method": "pin", "credential": PIN, "remember": False})
    cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith(SESSION_COOKIE))
    assert "Max-Age" not in cookie and "expires=" not in cookie.lower()


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://attacker.example", "X-Workspace-Access": "1"},
                                    {"Origin": ORIGIN}, {"Origin": "null", "X-Workspace-Access": "1"}])
def test_unlock_rejects_cross_origin_and_missing_csrf_headers(tmp_path, headers):
    client, _, _ = app_client(tmp_path)
    response = client.post("/api/workspace-access/unlock", headers=headers,
                           json={"method": "pin", "credential": PIN})
    assert response.status_code == 403 and not client.cookies.get(SESSION_COOKIE)


def test_unlock_requires_https_even_with_correct_origin_header(tmp_path):
    client, _, _ = app_client(tmp_path)
    response = client.post("http://phoney.example/api/workspace-access/unlock", headers=HEADERS,
                           json={"method": "pin", "credential": PIN})
    assert response.status_code == 403


@pytest.mark.parametrize("body", [
    '{"method":"pin","credential":"642815","credential":"000000"}',
    '{"method":[],"credential":"642815"}',
    '{"method":"pin","credential":"642815","remember":"true"}',
    '{"method":"pin","credential":"642815","return_to":"https://evil.example"}',
    '{"method":"pin","credential":"\\ud800"}',
    '[]',
])
def test_unlock_rejects_malformed_json_without_crashing(tmp_path, body):
    client, _, _ = app_client(tmp_path)
    response = client.post("/api/workspace-access/unlock", headers={**HEADERS, "Content-Type": "application/json"},
                           content=body)
    assert response.status_code == 400 and not client.cookies.get(SESSION_COOKIE)


def test_json_body_size_is_bounded(tmp_path):
    client, _, _ = app_client(tmp_path)
    response = client.post("/api/workspace-access/unlock", headers=HEADERS,
                           json={"method": "password", "credential": "x" * 5000})
    assert response.status_code == 413


def test_forwarded_headers_do_not_reset_ip_failures(tmp_path):
    client, _, _ = app_client(tmp_path)
    for i in range(3):
        response = client.post("/api/workspace-access/unlock",
                               headers={**HEADERS, "X-Forwarded-For": f"192.0.2.{i}",
                                        "CF-Connecting-IP": f"192.0.2.{i}"},
                               json={"method": "pin", "credential": "wrong"})
        assert response.status_code in {401, 403}
        client.cookies.clear()
    response = client.post("/api/workspace-access/unlock", headers=HEADERS,
                           json={"method": "pin", "credential": PIN})
    assert response.status_code == 403 and response.json()["error"] == "password_required"


def test_signed_twilio_body_reaches_service_but_unsigned_or_mutated_body_is_rejected(tmp_path):
    client, _, settings = app_client(tmp_path)
    form = {"AccountSid": settings.account_sid, "CallSid": "CA" + "b" * 32}
    signature = RequestValidator(settings.auth_token).compute_signature(ORIGIN + "/voice", form)
    response = client.post("/voice", data=form, headers={"X-Twilio-Signature": signature})
    assert response.status_code == 200 and response.json() == form
    assert client.post("/voice", data=form).status_code == 403
    assert client.post("/voice", data={**form, "CallSid": "CA" + "c" * 32},
                       headers={"X-Twilio-Signature": signature}).status_code == 403
    assert client.post("/voice/secret", data=form, headers={"X-Twilio-Signature": signature}).status_code == 401


def test_websocket_exemption_requires_a_valid_twilio_signature(tmp_path):
    from starlette.websockets import WebSocketDisconnect
    client, _, settings = app_client(tmp_path)
    path = "/media/CA" + "b" * 32 + "/"
    signature = RequestValidator(settings.auth_token).compute_signature(
        ORIGIN.replace("https://", "wss://") + path, {})
    with client.websocket_connect(path, headers={"X-Twilio-Signature": signature}) as socket:
        assert socket.receive_text() == "accepted"
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(path):
            pass
    assert error.value.code == 1008


def test_unconfigured_and_failed_storage_never_expose_private_content(tmp_path, monkeypatch):
    client, access, _ = app_client(tmp_path, configured=False)
    assert client.get("/api/workspace-access/status").status_code == 503
    assert client.get("/api/workspace").status_code != 200
    access.configure(PIN, PASSWORD)
    client.post("/api/workspace-access/unlock", headers=HEADERS,
                json={"method": "pin", "credential": PIN})
    def unavailable(*args):
        raise AccessUnavailable("Workspace access storage is unavailable.")
    monkeypatch.setattr(access, "authenticated", unavailable)
    response = client.get("/api/workspace")
    assert response.status_code == 503 and "workspace-content" not in response.text


def test_disabled_access_still_sends_noindex(tmp_path):
    client, _, _ = app_client(tmp_path, enabled=False)
    response = client.get("/dashboard")
    assert response.status_code == 200 and "noindex" in response.headers["x-robots-tag"]


def test_cli_explicit_env_file_overrides_inherited_storage_without_printing_secrets(tmp_path, monkeypatch, capsys):
    from scripts import workspace_access as cli
    workspace = tmp_path.resolve() / "explicit-storage"
    other = tmp_path.resolve() / "inherited-storage"
    env_file = tmp_path.resolve() / ".env"
    env_file.write_text(f"WORKSPACE_STORAGE_DIR={workspace}\nDATABASE_URL=\nWORKSPACE_ID=test-cli\n")
    monkeypatch.setenv("WORKSPACE_STORAGE_DIR", str(other))
    monkeypatch.setenv("DATABASE_URL", "must-not-use-this-secret-url")
    assert cli.main(["status", "--env-file", str(env_file)]) == 0
    output = capsys.readouterr()
    assert "not been configured" in output.out and "secret-url" not in output.out + output.err
    assert (workspace / "workspace_access.sqlite3").exists() and not other.exists()


def test_cli_set_refuses_noninteractive_secret_entry(tmp_path, monkeypatch):
    from scripts import workspace_access as cli
    env_file = tmp_path.resolve() / ".env"
    env_file.write_text(f"WORKSPACE_STORAGE_DIR={tmp_path.resolve()}\nDATABASE_URL=\n")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as error:
        cli.main(["set", "--env-file", str(env_file)])
    assert error.value.code == 2


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_sqlite_auth_rejects_linked_database_without_touching_target(tmp_path, kind):
    root = tmp_path.resolve() / "workspace"
    root.mkdir()
    target = tmp_path.resolve() / "target"
    target.write_text("unchanged")
    target.chmod(0o644)
    destination = root / "workspace_access.sqlite3"
    if kind == "symlink":
        destination.symlink_to(target)
    else:
        os.link(target, destination)
    with pytest.raises(AccessUnavailable):
        WorkspaceAccess(str(root))
    assert target.read_text() == "unchanged" and target.stat().st_mode & 0o777 == 0o644


def test_sqlite_auth_rejects_symlink_parent(tmp_path):
    real = tmp_path.resolve() / "real"
    real.mkdir()
    link = tmp_path.resolve() / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(AccessUnavailable):
        WorkspaceAccess(str(link))
    assert not (real / "workspace_access.sqlite3").exists()


def test_postgres_auth_persists_and_scopes_sessions():
    url = os.getenv("PHONEY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set PHONEY_TEST_DATABASE_URL for isolated PostgreSQL auth integration.")
    workspace = "test-auth-" + secrets.token_hex(8)
    access = WorkspaceAccess(database_url=url, workspace_id=workspace)
    try:
        access.configure(PIN, PASSWORD)
        _, _, token, device = unlock(access)
        reopened = WorkspaceAccess(database_url=url, workspace_id=workspace)
        assert reopened.authenticated(token)
        for _ in range(3):
            unlock(reopened, device, value="wrong")
        assert unlock(access, None)[1]["error"] == "password_required"
        assert access.authenticated(token)
        assert unlock(access, device, method="password", value=PASSWORD)[0] == 200
        reopened.configure("285146", "new recovery phrase 743")
        assert not access.authenticated(token)
    finally:
        with access._transaction() as db:
            for table in ("workspace_access_guards", "workspace_access_sessions", "workspace_access_credentials"):
                db.execute(f"DELETE FROM {table} WHERE workspace_id=?", (workspace,))
