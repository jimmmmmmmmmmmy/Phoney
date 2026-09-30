"""Explicit public callback dialing remains same-origin and owner-first."""

from dataclasses import replace
import uuid

import pytest

from operator_service.sessions import OWNER, OWNER_PROMPT
from test_operator_routes import (
    DESTINATION, HEADERS, OWNER_NUMBER, SETTINGS, bridge_client, connect, connected,
    keypad, session_of, settle, start_message, until,
)
from test_outbound_call_config import managed_settings, owner_headers, unlock


PUBLIC = replace(SETTINGS, public_calling_enabled=True, allowed_destinations=(),
                 allowed_destination_countries=("US",))
PUBLIC_FIELDS = {"id", "direction", "to", "phase", "ended_reason"}


def start(client, settings, *, key=None, to=DESTINATION):
    return client.post("/api/calls/outbound", json={"to": to, "goal": "private calling goal"},
        headers={**owner_headers(settings), "Idempotency-Key": key or str(uuid.uuid4())})


def test_public_callback_needs_no_owner_registry_but_still_waits_for_owner_press_one():
    with bridge_client(PUBLIC) as (client, dialer, settings):
        config = client.get("/api/calls/config").json()
        assert config == {"authenticated": False, "public_calling": True, "enabled": True,
            "owner_label": "•••• 0101", "destinations": [], "countries": ["US"],
            "active_session": None, "busy": False}
        key = str(uuid.uuid4())
        response = start(client, settings, key=key)
        assert response.status_code == 202
        session_id = response.json()["session_id"]
        settle(client)
        assert len(dialer.created) == 1 and dialer.created[0]["to"] == OWNER_NUMBER
        assert start(client, settings, key=key).json()["duplicate"] is True
        assert start(client, settings, to="+12025559999").status_code == 429
        assert client.get("/api/calls/config").json()["active_session"]["id"] == session_id
        with connect(client, settings, session_id, OWNER) as owner:
            owner.send_json(connected())
            owner.send_json(start_message(client, session_id, OWNER))
            until(client, lambda: session_of(client, session_id).phase == OWNER_PROMPT)
            owner.send_json(keypad("5", 2))
            settle(client)
            assert len(dialer.created) == 1
            owner.send_json(keypad("1", 3))
            until(client, lambda: len(dialer.created) == 2)
            assert dialer.created[1]["to"] == DESTINATION
            assert client.get(f"/api/sessions/{session_id}").json()["phase"] == "remote_setup"
        assert not client.cookies


def test_public_status_config_and_end_never_expose_owner_session_details():
    with bridge_client(PUBLIC) as (client, dialer, settings):
        session_id = start(client, settings).json()["session_id"]
        settle(client)
        session = session_of(client, session_id)
        session.turns.append({"speaker": "owner", "text": "private transcript"})
        session.summary = "private summary"
        session.agent_name = "private agent"
        public_status = client.get(f"/api/sessions/{session_id}")
        active = client.get("/api/calls/config").json()["active_session"]
        assert set(public_status.json()) == set(active) == PUBLIC_FIELDS
        admin = client.get(f"/api/sessions/{session_id}", headers=HEADERS)
        assert admin.status_code == 200 and admin.json()["goal"] == "private calling goal"
        assert admin.json()["turns"][0]["text"] == "private transcript"
        ended = client.post(f"/api/sessions/{session_id}/end", headers=owner_headers(settings))
        assert ended.status_code == 200 and ended.json()["phase"] == "ended"
        assert set(ended.json()) == PUBLIC_FIELDS
        for response in (public_status, ended, client.get("/api/calls/config")):
            assert response.headers["cache-control"] == "no-store"
            for private in ("private transcript", "private calling goal", "private summary", "private agent",
                            OWNER_NUMBER, DESTINATION, session.legs[OWNER].token,
                            session.legs[OWNER].call_sid):
                assert private not in response.text
        settle(client)
        assert dialer.ended == [session.legs[OWNER].call_sid]
        assert client.get("/api/calls/config").json()["active_session"] is None


@pytest.mark.parametrize("headers", [
    {}, {"X-Agent-Request": "1"}, {"Origin": SETTINGS.public_base_url},
    {"Origin": "https://outside.example", "X-Agent-Request": "1"},
    {"Origin": "null", "X-Agent-Request": "1"},
    {"Origin": "http://operator.example", "X-Agent-Request": "1"},
    {"Origin": SETTINGS.public_base_url, "X-Agent-Request": "0"},
])
def test_public_start_and_end_require_same_origin_request_marker(headers):
    with bridge_client(PUBLIC) as (client, dialer, settings):
        response = client.post("/api/calls/outbound", json={"to": DESTINATION},
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())})
        assert response.status_code == 403
        assert dialer.created == []
        session_id = start(client, settings).json()["session_id"]
        settle(client)
        assert client.post(f"/api/sessions/{session_id}/end", headers=headers).status_code == 403
        assert session_of(client, session_id).active
        assert dialer.ended == []


def test_public_dialing_does_not_unlock_operator_listing_takeover_or_mode():
    with bridge_client(PUBLIC) as (client, dialer, settings):
        session_id = start(client, settings).json()["session_id"]
        headers = owner_headers(settings)
        assert client.get("/api/operator/sessions").status_code == 403
        assert client.post(f"/api/sessions/{session_id}/takeover", json={"slot": "1"},
                           headers=headers).status_code == 403
        assert client.post(f"/api/sessions/{session_id}/mode", json={"mode": "human"},
                           headers=headers).status_code == 403


def test_public_dialing_preserves_us_and_loop_restrictions():
    with bridge_client(PUBLIC) as (client, dialer, settings):
        for destination in ("+14165550123", "+18095550123", "+442083661177", "+12001230101",
                            settings.owner_number, settings.twilio_number):
            assert start(client, settings, to=destination).status_code == 403
        assert client.post("/api/calls/outbound", json={"to": DESTINATION},
                           headers=owner_headers(settings)).status_code == 400
        settle(client)
        assert dialer.created == []


def test_public_calling_cannot_start_discover_attach_or_end_owner_browser_call():
    with bridge_client(PUBLIC) as (client, dialer, settings):
        assert start(client, settings, to=OWNER_NUMBER).status_code == 403
        started = client.post("/api/calls/outbound", json={"to": OWNER_NUMBER},
            headers={**HEADERS, "Idempotency-Key": str(uuid.uuid4())})
        assert started.status_code == 202
        session_id = started.json()["session_id"]
        settle(client)
        assert dialer.created == []
        public = client.get("/api/calls/config").json()
        assert public["busy"] is True and public["active_session"] is None
        assert "self_call_number" not in public
        assert client.get(f"/api/sessions/{session_id}").status_code == 403
        assert client.post(f"/api/sessions/{session_id}/end", headers=owner_headers(settings)).status_code == 403
        assert client.post(f"/api/sessions/{session_id}/browser-token", headers=owner_headers(settings)).status_code == 403
        assert session_of(client, session_id).active
        assert start(client, settings, to=OWNER_NUMBER, key=started.request.headers["Idempotency-Key"]).status_code == 403


def test_public_access_cannot_read_or_end_inbound_but_real_owner_still_can(tmp_path):
    settings = managed_settings(tmp_path, public_calling_enabled=True, allowed_destinations=(),
        allowed_destination_countries=("US",), operator_inbound_enabled=True,
        voice_agent_enabled=True, media_capture_enabled=True, media_storage_dir=str(tmp_path / "audio"),
        transcription_enabled=True, deepgram_api_key="offline-deepgram-key",
        transcript_storage_dir=str(tmp_path / "transcripts"))
    with bridge_client(settings) as (client, dialer, settings):
        session, _ = client.portal.call(client.app.state.operator.reserve_inbound,
                                       "CA" + "2" * 32, DESTINATION)
        config = client.get("/api/calls/config").json()
        assert config["busy"] is True and config["active_session"] is None
        assert client.get(f"/api/sessions/{session.id}").status_code == 403
        assert client.post(f"/api/sessions/{session.id}/end",
                           headers=owner_headers(settings)).status_code == 403
        assert session.active
        unlock(client, settings)
        assert client.get("/api/calls/config").json()["authenticated"] is True
        full = client.get(f"/api/sessions/{session.id}")
        assert full.status_code == 200 and full.json()["direction"] == "inbound"
        assert "legs" in full.json()
        assert client.post(f"/api/sessions/{session.id}/end",
                           headers=owner_headers(settings)).status_code == 200
        assert not session.active


def test_public_opt_in_does_not_enable_an_unconfigured_bridge():
    with bridge_client(replace(PUBLIC, allowed_destination_countries=())) as (client, dialer, settings):
        config = client.get("/api/calls/config").json()
        assert config["public_calling"] is True and config["enabled"] is False
        assert start(client, settings).status_code == 503
        assert dialer.created == []
