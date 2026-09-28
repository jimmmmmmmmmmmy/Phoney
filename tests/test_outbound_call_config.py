"""Dialer discovery and owner-cookie boundaries; all calls use local fakes."""

from dataclasses import replace
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_registry import AgentRegistry, OWNER_COOKIE
from operator_service import OperatorSessions, register_operator_routes
from operator_service.sessions import CONNECTED, OWNER, OWNER_PROMPT, REMOTE
from test_operator_routes import (
    ADMIN_TOKEN, DESTINATION, HEADERS, OWNER_NUMBER, SETTINGS, bridge_client,
    Dialer, connect, connected, keypad, session_of, settle, start_call, start_message, until,
)


def managed_settings(tmp_path, **changes):
    return replace(SETTINGS, agent_management_enabled=True, agent_demo_mode=True,
                   workspace_storage_dir=str(tmp_path / "workspace"), **changes)


def owner_headers(settings):
    return {"Origin": settings.public_base_url, "X-Agent-Request": "1"}


def unlock(client, settings):
    registry = client.app.state.agent_registry
    result = client.post("/api/agents/session", json={"code": registry.grant()},
                         headers=owner_headers(settings))
    assert result.status_code == 200


def test_demo_access_and_bearer_do_not_disclose_owner_call_configuration(tmp_path):
    with bridge_client(managed_settings(tmp_path)) as (client, dialer, settings):
        assert client.get("/api/agents/config").json()["authenticated"] is True
        assert start_call(client).status_code == 202
        settle(client)
        for headers in ({}, HEADERS, {"X-Agent-Request": "1"}):
            response = client.get("/api/calls/config", headers=headers)
            assert response.status_code == 200
            assert response.json() == {"authenticated": False, "enabled": True,
                "owner_label": None, "destinations": [], "countries": [],
                "active_session": None, "busy": False}
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["referrer-policy"] == "no-referrer"
            for private in (OWNER_NUMBER, DESTINATION, ADMIN_TOKEN, "itemized"):
                assert private not in response.text
        client.cookies.set(OWNER_COOKIE, "invalid-owner-cookie")
        assert client.get("/api/calls/config").json()["authenticated"] is False
        assert len(dialer.created) == 1


def test_owner_can_recover_ringing_call_without_origin_and_end_with_csrf_headers(tmp_path):
    with bridge_client(managed_settings(tmp_path)) as (client, dialer, settings):
        unlock(client, settings)
        before = client.get("/api/calls/config").json()
        assert before == {"authenticated": True, "enabled": True,
            "owner_label": "•••• 0101", "destinations": [DESTINATION],
            "countries": [], "active_session": None, "busy": False}
        key = str(uuid.uuid4())
        headers = {**owner_headers(settings), "Idempotency-Key": key}
        started = client.post("/api/calls/outbound", json={"to": DESTINATION}, headers=headers)
        assert started.status_code == 202
        session_id = started.json()["session_id"]
        settle(client)
        response = client.get("/api/calls/config")
        state = response.json()
        assert state["busy"] is True
        assert state["active_session"]["id"] == session_id
        assert state["active_session"]["phase"] == "owner_ringing"
        assert OWNER_NUMBER not in response.text
        assert ADMIN_TOKEN not in response.text
        session = session_of(client, session_id)
        assert session.legs[OWNER].token not in response.text
        assert client.get(f"/api/sessions/{session_id}").status_code == 200
        assert client.get("/api/operator/sessions").status_code == 200
        assert client.post(f"/api/sessions/{session_id}/end").status_code == 403
        assert client.post(f"/api/sessions/{session_id}/end",
                           headers={"Origin": "https://external.example", "X-Agent-Request": "1"}).status_code == 403
        assert session.active
        ended = client.post(f"/api/sessions/{session_id}/end", headers=owner_headers(settings))
        assert ended.status_code == 200
        settle(client)
        after = client.get("/api/calls/config").json()
        assert after["active_session"] is None and after["busy"] is False
        assert len(dialer.created) == 1
        assert dialer.ended == [session.legs[OWNER].call_sid]
        assert client.delete("/api/agents/session", headers=owner_headers(settings)).status_code == 200
        assert client.get("/api/calls/config").json()["authenticated"] is False
        assert client.get(f"/api/sessions/{session_id}").status_code == 403
        assert client.get("/api/operator/sessions").status_code == 403


def test_config_recovers_each_outbound_setup_phase_and_connected_call(tmp_path):
    with bridge_client(managed_settings(tmp_path)) as (client, dialer, settings):
        unlock(client, settings)
        store = client.app.state.operator
        session, _ = client.portal.call(store.reserve_outbound, DESTINATION, "", str(uuid.uuid4()))
        assert client.get("/api/calls/config").json()["active_session"]["phase"] == "reserved"
        client.portal.call(client.app.state.operator_controller._dial_owner, session.id)
        with connect(client, settings, session.id, OWNER) as owner:
            owner.send_json(connected())
            owner.send_json(start_message(client, session.id, OWNER))
            until(client, lambda: session.phase == OWNER_PROMPT)
            assert client.get("/api/calls/config").json()["active_session"]["phase"] == "owner_prompt"
            owner.send_json(keypad("1", 2))
            until(client, lambda: len(dialer.created) == 2)
            assert client.get("/api/calls/config").json()["active_session"]["phase"] == "remote_setup"
            with connect(client, settings, session.id, REMOTE) as remote:
                remote.send_json(connected())
                remote.send_json(start_message(client, session.id, REMOTE))
                until(client, lambda: session.phase == CONNECTED)
                assert client.get("/api/calls/config").json()["active_session"]["phase"] == "connected"
                assert client.get("/api/operator/sessions").json()["sessions"][0]["id"] == session.id


@pytest.mark.parametrize("changes", [
    {"allowed_destinations": ()}, {"owner_number": ""}, {"operator_admin_token": ""},
    {"twilio_number": ""},
])
def test_dialer_reports_missing_required_configuration_without_placing_calls(tmp_path, changes):
    with bridge_client(managed_settings(tmp_path, **changes)) as (client, dialer, settings):
        assert client.get("/api/calls/config").json()["enabled"] is False
        unlock(client, settings)
        data = client.get("/api/calls/config").json()
        assert data["authenticated"] is True and data["enabled"] is False
        assert data["destinations"] == list(settings.allowed_destinations)
        assert data["active_session"] is None and data["busy"] is False
        assert dialer.created == []


def test_owner_cookie_routes_do_not_allow_demo_access_or_missing_csrf(tmp_path):
    with bridge_client(managed_settings(tmp_path)) as (client, dialer, settings):
        headers = {**owner_headers(settings), "Idempotency-Key": str(uuid.uuid4())}
        assert client.post("/api/calls/outbound", json={"to": DESTINATION}, headers=headers).status_code == 403
        assert client.get("/api/operator/sessions").status_code == 403
        unlock(client, settings)
        assert client.post("/api/calls/outbound", json={"to": DESTINATION},
                           headers={"Idempotency-Key": str(uuid.uuid4())}).status_code == 403
        assert client.post("/api/calls/outbound", json={"to": "+12025559999"}, headers=headers).status_code == 403
        settle(client)
        assert dialer.created == []


def test_inbound_call_makes_dialer_busy_without_becoming_an_outbound_control(tmp_path):
    settings = managed_settings(tmp_path, allowed_destinations=(), operator_inbound_enabled=True,
        voice_agent_enabled=True, media_capture_enabled=True, media_storage_dir=str(tmp_path / "audio"),
        transcription_enabled=True, deepgram_api_key="offline-deepgram-key",
        transcript_storage_dir=str(tmp_path / "transcripts"))
    with bridge_client(settings) as (client, dialer, settings):
        store = client.app.state.operator
        assert store.ready
        client.portal.call(store.reserve_inbound, "CA" + "2" * 32, DESTINATION)
        assert client.get("/api/calls/config").json()["busy"] is False
        unlock(client, settings)
        state = client.get("/api/calls/config").json()
        assert state["enabled"] is False  # Inbound readiness does not authorize any destination.
        assert state["busy"] is True and state["active_session"] is None
        assert state["destinations"] == []
        assert dialer.created == []


def test_disabled_agent_management_has_no_owner_calling_ui():
    with bridge_client() as (client, dialer, settings):
        assert client.get("/api/calls/config").json() == {"authenticated": False, "enabled": False,
            "owner_label": None, "destinations": [], "countries": [],
            "active_session": None, "busy": False}
        assert dialer.created == []


@pytest.mark.parametrize("registry", [None, AgentRegistry("")])
def test_missing_owner_registry_disables_calling_ui(tmp_path, registry):
    settings = managed_settings(tmp_path)
    app = FastAPI()
    dialer = Dialer()
    register_operator_routes(app, settings, OperatorSessions(settings), dialer=dialer, registry=registry)
    with TestClient(app, base_url=settings.public_base_url) as client:
        data = client.get("/api/calls/config").json()
        assert data["enabled"] is False and data["authenticated"] is False
        assert data["countries"] == data["destinations"] == []
        assert dialer.created == []


def test_us_policy_allows_only_authenticated_owner_to_start_valid_us_callback(tmp_path):
    settings = managed_settings(tmp_path, allowed_destinations=(), allowed_destination_countries=("US",))
    with bridge_client(settings) as (client, dialer, settings):
        public = client.get("/api/calls/config").json()
        assert public["enabled"] is True and public["authenticated"] is False
        assert public["countries"] == public["destinations"] == []
        headers = {**owner_headers(settings), "Idempotency-Key": str(uuid.uuid4())}
        assert client.post("/api/calls/outbound", json={"to": DESTINATION}, headers=headers).status_code == 403
        unlock(client, settings)
        config = client.get("/api/calls/config").json()
        assert config["authenticated"] is True and config["enabled"] is True
        assert config["countries"] == ["US"] and config["destinations"] == []
        for destination in ("+14165550123", "+18095550123", "+442083661177", "+12001230101",
                            settings.owner_number, settings.twilio_number):
            response = client.post("/api/calls/outbound", json={"to": destination}, headers=headers)
            assert response.status_code == 403
        settle(client)
        assert dialer.created == []
        result = client.post("/api/calls/outbound", json={"to": DESTINATION}, headers=headers)
        assert result.status_code == 202
        settle(client)
        assert len(dialer.created) == 1 and dialer.created[0]["to"] == OWNER_NUMBER
        assert client.post("/api/calls/outbound", json={"to": DESTINATION}, headers=headers).json()["duplicate"]
        settle(client)
        assert len(dialer.created) == 1
