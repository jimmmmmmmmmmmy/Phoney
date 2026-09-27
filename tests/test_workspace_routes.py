"""Shared workspace writes are durable, bounded, and restricted to our own UI."""

from copy import deepcopy
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from dashboard import register_dashboard
from tests.test_dashboard import Manager, SETTINGS
from workspace_store import WorkspaceStore


BASE = "https://dashboard.example"
CONTACT = "local-12345678-1234-1234-1234-123456789abc"
OTHER_CONTACT = "local-abcdefgh-1234-1234-1234-123456789abc"
AGENT = "agent-12345678-1234-1234-1234-123456789abc"
OTHER_AGENT = "agent-abcdefgh-1234-1234-1234-123456789abc"
HEADERS = {"Origin": BASE, "X-Workspace-Request": "1"}
EMPTY = {"version": 1, "contacts": [], "demoOverrides": [], "agents": []}
IMPORT_EMPTY = {"contacts": [], "demoOverrides": [], "agents": []}


def contact(**changes):
    return {"id": CONTACT, "firstName": "Shane", "lastName": "McCarthy",
            "phone": "+19415551234", "email": "shane@example.com", "address": "Sarasota, FL",
            "website": "https://example.com", "company": "Example Studio",
            "createdAt": "2026-09-26T18:30:00Z", "status": "New", "labels": ["Customers"],
            "demo": changes.get("id", CONTACT).startswith("demo-"),
            **changes}


def agent(**changes):
    return {"id": AGENT, "name": "Reception", "prompt": "Ask how we can help.",
            "createdAt": "2026-09-26T18:30:00Z", **changes}


def client_for(store=None, base_url=BASE):
    app = FastAPI()
    register_dashboard(app, SETTINGS, Manager(), workspace_store=store)
    return TestClient(app, base_url=base_url)


def put_contact(client, value=None, **kwargs):
    value = contact() if value is None else value
    return client.put("/api/workspace/contacts/" + value["id"], json=value,
                      headers=HEADERS, **kwargs)


def put_agent(client, value=None):
    value = agent() if value is None else value
    return client.put("/api/workspace/agents/" + value["id"], json=value, headers=HEADERS)


def assert_error(response, status):
    assert response.status_code == status
    assert set(response.json()) == {"detail"}
    assert isinstance(response.json()["detail"], str)
    assert response.json()["detail"]
    assert "access-control-allow-origin" not in response.headers


def test_empty_workspace_is_readable_without_browser_credentials(tmp_path):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        response = client.get("/api/workspace")
        assert response.status_code == 200
        assert response.json() == EMPTY
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
        assert SETTINGS.auth_token not in response.text
        assert SETTINGS.deepgram_api_key not in response.text


def test_contact_agent_and_edits_survive_restart_and_reach_another_browser(tmp_path):
    directory = str(tmp_path / "shared")
    with client_for(WorkspaceStore(directory)) as first, client_for(WorkspaceStore(directory)) as second:
        response = put_contact(first)
        assert response.status_code == 200
        assert response.json() == {**contact(), "revision": 1}
        assert put_agent(first).json() == {**agent(), "revision": 1}
        # A store that existed before the write must observe other browser edits.
        assert second.get("/api/workspace").json() == {
            "version": 1, "contacts": [{**contact(), "revision": 1}], "demoOverrides": [], "agents": [{**agent(), "revision": 1}]}
        changed = contact(firstName="Updated", labels=["Legal"], revision=1)
        changed = put_contact(second, changed).json()
        assert changed["revision"] == 2
        assert first.get("/api/workspace").json()["contacts"] == [changed]
    with client_for(WorkspaceStore(directory), base_url="https://new-tunnel.example") as restarted:
        assert restarted.get("/api/workspace").json()["contacts"] == [changed]
        updated_agent = agent(prompt="Confirm the appointment time.", revision=1)
        response = restarted.put("/api/workspace/agents/" + AGENT, json=updated_agent,
                                 headers={"Origin": "https://new-tunnel.example", "X-Workspace-Request": "1"})
        assert response.status_code == 200 and response.json() == {**updated_agent, "revision": 2}


@pytest.mark.parametrize("origin", [BASE, "https://operator.example"])
def test_current_or_configured_origin_can_write(tmp_path, origin):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        response = client.put("/api/workspace/contacts/" + CONTACT, json=contact(),
                              headers={"Origin": origin, "X-Workspace-Request": "1"})
        assert response.status_code == 200


@pytest.mark.parametrize("headers", [
    {}, {"X-Workspace-Request": "1"}, {"Origin": BASE},
    {"Origin": BASE, "X-Workspace-Request": "0"},
    {"Origin": "https://external.example", "X-Workspace-Request": "1"},
    {"Origin": "https://operator.example.evil.invalid", "X-Workspace-Request": "1"},
    {"Origin": "null", "X-Workspace-Request": "1"},
    {"Origin": "http://dashboard.example", "X-Workspace-Request": "1"},
])
def test_cross_origin_missing_origin_and_missing_ui_marker_cannot_write(tmp_path, headers):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        for path, payload in [("/contacts/" + CONTACT, contact()), ("/agents/" + AGENT, agent())]:
            assert_error(client.put("/api/workspace" + path, json=payload, headers=headers), 403)
        assert_error(client.post("/api/workspace/import", json=IMPORT_EMPTY, headers=headers), 403)
        assert client.get("/api/workspace").json() == EMPTY


def test_import_preflight_is_not_cors_enabled(tmp_path):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        response = client.options("/api/workspace/import", headers={
            "Origin": "https://external.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-workspace-request"})
        assert response.status_code in {403, 405}
        assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_writes_require_json_content_type(tmp_path, content_type):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        headers = dict(HEADERS)
        if content_type is not None:
            headers["Content-Type"] = content_type
        response = client.put("/api/workspace/contacts/" + CONTACT,
                              content=json.dumps(contact()), headers=headers)
        assert_error(response, 415)
        assert client.get("/api/workspace").json() == EMPTY


def test_json_charset_is_accepted(tmp_path):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        response = client.put("/api/workspace/contacts/" + CONTACT, content=json.dumps(contact()),
                              headers={**HEADERS, "Content-Type": "application/json; charset=utf-8"})
        assert response.status_code == 200


@pytest.mark.parametrize("payload", [b"{", b"[]", b"null", b'"not an object"'])
def test_invalid_json_and_wrong_top_level_types_are_rejected(tmp_path, payload):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        response = client.put("/api/workspace/contacts/" + CONTACT, content=payload,
                              headers={**HEADERS, "Content-Type": "application/json"})
        assert_error(response, 400)
        assert client.get("/api/workspace").json() == EMPTY


@pytest.mark.parametrize("changes", [
    {"firstName": " "}, {"lastName": ""}, {"phone": "not a phone"},
    {"phone": True}, {"website": "javascript:alert(1)"}, {"createdAt": "yesterday"},
    {"labels": ["x"] * 11}, {"status": "not a status"}, {"id": "local-short"},
    {"phone": "9415551234"}, {"phone": "19415551234"},
])
def test_invalid_contacts_are_atomic_client_errors(tmp_path, changes):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        assert_error(put_contact(client, contact(**changes)), 400)
        assert client.get("/api/workspace").json() == EMPTY


@pytest.mark.parametrize("changes", [{"name": " "}, {"prompt": 42}, {"prompt": "x" * 8001},
                                     {"id": "agent-x"}])
def test_invalid_agents_are_atomic_client_errors(tmp_path, changes):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        assert_error(put_agent(client, agent(**changes)), 400)
        assert client.get("/api/workspace").json() == EMPTY


def test_route_identity_cannot_be_changed_in_request_body(tmp_path):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        assert_error(client.put("/api/workspace/contacts/" + OTHER_CONTACT,
                                json=contact(), headers=HEADERS), 400)
        assert_error(client.put("/api/workspace/agents/" + OTHER_AGENT,
                                json=agent(), headers=HEADERS), 400)


@pytest.mark.parametrize("phone", ["+1 (941) 555-1234", "+1-941-555-1234", "+1 941 555 1234"])
def test_phone_normalization_prevents_duplicate_contacts(tmp_path, phone):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        response = put_contact(client, contact(phone=phone))
        assert response.status_code == 200
        assert response.json()["phone"] == "+19415551234"
        assert_error(put_contact(client, contact(id=OTHER_CONTACT)), 409)
        assert len(client.get("/api/workspace").json()["contacts"]) == 1


def test_demo_contact_edits_are_shared_overrides(tmp_path):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        updated = contact(id="demo-alex-morgan", firstName="Alex", lastName="Morgan",
                          phone="+19415550101", labels=["Real Estate"])
        assert put_contact(client, updated).status_code == 200
        snapshot = client.get("/api/workspace").json()
        assert snapshot["contacts"] == []
        assert snapshot["demoOverrides"] == [{**updated, "revision": 1}]


@pytest.mark.parametrize("chunked", [False, True])
def test_oversized_body_rejected_with_or_without_content_length(tmp_path, chunked):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        raw = b" " * (2 * 1024 * 1024 + 1) + b"{}"
        body = (raw[index:index + 65536] for index in range(0, len(raw), 65536)) if chunked else raw
        response = client.post("/api/workspace/import", content=body,
                               headers={**HEADERS, "Content-Type": "application/json"})
        assert_error(response, 413)
        assert client.get("/api/workspace").json() == EMPTY


def test_disabled_workspace_returns_service_unavailable():
    with client_for() as client:
        assert_error(client.get("/api/workspace"), 503)
        assert_error(put_contact(client), 503)
        assert_error(put_agent(client), 503)
        assert_error(client.post("/api/workspace/import", json=IMPORT_EMPTY, headers=HEADERS), 503)


def test_storage_failures_are_sanitized_service_errors():
    class BrokenStore:
        def __getattr__(self, name):
            def broken(*args, **kwargs):
                raise OSError("private filesystem path and credentials")
            return broken
    with client_for(BrokenStore()) as client:
        responses = [client.get("/api/workspace"), put_contact(client), put_agent(client),
                     client.post("/api/workspace/import", json=IMPORT_EMPTY, headers=HEADERS)]
        for response in responses:
            assert_error(response, 503)
            assert "private" not in response.text and "credentials" not in response.text


def test_invalid_import_does_not_partially_write_valid_records(tmp_path):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        payload = {"contacts": [contact()], "demoOverrides": [], "agents": [agent(name="")]}
        assert_error(client.post("/api/workspace/import", json=payload, headers=HEADERS), 400)
        assert client.get("/api/workspace").json() == EMPTY


def test_stale_import_never_overwrites_shared_edits_or_duplicate_phone(tmp_path):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        current = contact(firstName="Current shared name")
        assert put_contact(client, current).status_code == 200
        current_agent = agent(prompt="Current shared prompt")
        assert put_agent(client, current_agent).status_code == 200
        payload = {"contacts": [contact(), contact(id=OTHER_CONTACT)], "demoOverrides": [],
                   "agents": [agent(), agent(id=OTHER_AGENT, prompt=current_agent["prompt"])]}
        response = client.post("/api/workspace/import", json=payload, headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["contacts"] == [{**current, "revision": 1}]
        assert response.json()["agents"] == [{**current_agent, "revision": 1}]
        assert client.get("/api/workspace").json() == response.json()


def test_legacy_agent_import_has_stable_identity_and_is_idempotent_after_restart(tmp_path):
    directory = str(tmp_path / "shared")
    legacy = {"name": "Legacy reception", "prompt": "Take a message.", "createdAt": "2026-08-01T12:00:00Z"}
    payload = {"contacts": [contact()], "demoOverrides": [], "agents": [legacy, deepcopy(legacy)]}
    with client_for(WorkspaceStore(directory)) as client:
        first = client.post("/api/workspace/import", json=payload, headers=HEADERS)
        assert first.status_code == 200
        snapshot = first.json()
        assert len(snapshot["agents"]) == 1
        assert snapshot["agents"][0]["id"].startswith("agent-")
        assert snapshot["agents"][0]["name"] == legacy["name"]
        assert len(snapshot["contacts"]) == 1
    with client_for(WorkspaceStore(directory), base_url="https://replacement-tunnel.example") as client:
        response = client.post("/api/workspace/import", json=payload, headers={
            "Origin": "https://replacement-tunnel.example", "X-Workspace-Request": "1"})
        assert response.status_code == 200
        assert response.json() == snapshot


def test_legacy_browser_metadata_and_undated_agents_import_cleanly(tmp_path):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        legacy_contact = contact(note="An older browser-only note.")
        legacy_demo = contact(id="demo-alex-morgan", firstName="Alex", lastName="Morgan",
                              phone="+19415550101", note="Old demo note.")
        legacy_agents = [{"name": "Undated draft", "prompt": "Take a message."},
                         {"name": "Undated draft", "prompt": "Take a message.", "createdAt": ""}]
        response = client.post("/api/workspace/import", headers=HEADERS, json={
            "contacts": [legacy_contact], "demoOverrides": [legacy_demo], "agents": legacy_agents})
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["contacts"] == [{**contact(), "revision": 1}]
        assert snapshot["demoOverrides"][0]["demo"] is True
        assert "note" not in snapshot["demoOverrides"][0]
        assert len(snapshot["agents"]) == 1 and snapshot["agents"][0]["createdAt"] == ""


def test_browser_cannot_overwrite_newer_contact_or_draft(tmp_path):
    with client_for(WorkspaceStore(str(tmp_path / "shared"))) as client:
        for kind, value, field in (("contacts", contact(), "firstName"), ("agents", agent(), "prompt")):
            path = "/api/workspace/" + kind + "/" + value["id"]
            first = client.put(path, json=value, headers=HEADERS).json()
            second = client.put(path, json={**first, field: "Latest"}, headers=HEADERS).json()
            rejected = client.put(path, json={**first, field: "Stale"}, headers=HEADERS)
            assert_error(rejected, 409)
            assert "changed in another browser" in rejected.json()["detail"]
            assert client.get("/api/workspace").json()[kind] == [second]
