"""Focused product and boundary checks; test helpers live in support."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3

from twilio.request_validator import RequestValidator
import pytest

from workspace_auth import SESSION_SECONDS, WorkspaceAccess
from workspace_auth.web import SESSION_COOKIE

from support.workspace_auth import HEADERS, ORIGIN, PASSWORD, PIN, access, app_client, unlock


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


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://attacker.example", "X-Workspace-Access": "1"},
                                    {"Origin": ORIGIN}, {"Origin": "null", "X-Workspace-Access": "1"}])
def test_unlock_rejects_cross_origin_and_missing_csrf_headers(tmp_path, headers):
    client, _, _ = app_client(tmp_path)
    response = client.post("/api/workspace-access/unlock", headers=headers,
                           json={"method": "pin", "credential": PIN})
    assert response.status_code == 403 and not client.cookies.get(SESSION_COOKIE)


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


def test_parallel_attempts_cannot_exceed_three_pin_checks(access):
    _, device = access.status(None, "192.0.2.1")
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: unlock(access, device, value="wrong"), range(6)))
    assert sum(result[1]["error"] == "pin_invalid" for result in results) == 2
    assert sum(result[1]["error"] == "password_required" for result in results) == 4
