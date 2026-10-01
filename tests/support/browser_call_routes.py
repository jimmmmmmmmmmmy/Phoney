"""Shared fixtures and fakes for focused integration checks."""

from support.operator_routes import (
    HEADERS,
    OWNER_NUMBER,
    SETTINGS,
    next_event,
    start_call,
)


def self_call(client, key=None):
    response = start_call(client, key=key, to=OWNER_NUMBER)
    assert response.status_code == 202, response.text
    assert response.json()["browser_audio"] is True
    return response.json()["session_id"]


def browser_token(client, session_id, headers=HEADERS):
    response = client.post(f"/api/sessions/{session_id}/browser-token", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    grant = response.json()
    assert grant["url"] == f"/browser-media/{session_id}/"
    assert len(grant["token"]) >= 32
    return grant


def browser_connect(client, grant, origin=SETTINGS.public_base_url):
    headers = {} if origin is None else {"Origin": origin}
    return client.websocket_connect(grant["url"], headers=headers)


def browser_start(client, socket, grant):
    socket.send_json({"event": "start", "token": grant["token"]})
    assert next_event(client, socket)["event"] == "ready"
