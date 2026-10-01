"""Native browser calling uses a signed SDK leg and a passive observation tap.

All provider calls are local fakes. These tests exercise actual routes, the
session store and the conference controller without placing telephone calls.
"""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import uuid
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from app import create_app
from operator_service.sessions import CONNECTED, ENDED, OWNER, REMOTE, TOKEN_SECONDS, OperatorRejected
from test_media_webhooks import Gateway, signed_post, socket_headers
from test_operator_routes import (DESTINATION, HEADERS, OWNER_NUMBER, OWNER_SID,
                                  OWNER_STREAM, REMOTE_SID, SETTINGS, Dialer,
                                  session_of, settle, start_call, until)
from workspace_auth.web import SESSION_COOKIE


SDK_SETTINGS = replace(SETTINGS, browser_voice_enabled=True,
    api_key="SK" + "9" * 32, api_secret="local-test-api-secret-32-characters",
    twilio_browser_app_sid="AP" + "8" * 32,
    twilio_conference_app_sid="AP" + "7" * 32)
CONFERENCE = "CF" + "6" * 32
OTHER_SID = "CA" + "5" * 32


class SDKDialer(Dialer):
    def __init__(self):
        super().__init__()
        self.sid = REMOTE_SID
        self.mutes, self.ended_conferences = [], []

    async def mute_participant(self, conference_sid, call_sid, muted):
        self.mutes.append((conference_sid, call_sid, muted))

    async def end_conference(self, conference_sid):
        self.ended_conferences.append(conference_sid)


@contextmanager
def sdk_client(settings=SDK_SETTINGS):
    dialer = SDKDialer()
    with TestClient(create_app(settings, gateway=Gateway(), operator_dialer=dialer),
                    base_url=settings.public_base_url) as client:
        yield client, dialer, settings


def reserve(client, *, to=DESTINATION, key=None, headers=HEADERS):
    return client.post("/api/calls/outbound",
        headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())},
        json={"to": to, "browser_audio": True})


def grant(client, call):
    response = client.post(f"/api/sessions/{call.id}/browser-token", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    value = response.json()
    assert value["transport"] == "twilio-voice-sdk"
    assert set(value["params"]) == {"SessionId", "Token"}
    assert value["params"]["SessionId"] == call.id
    return value


def app_form(call, value=None, settings=SDK_SETTINGS, **updates):
    form = {"AccountSid": settings.account_sid, "CallSid": OWNER_SID,
            "From": "client:phoney_" + call.id,
            "ApplicationSid": settings.twilio_browser_app_sid}
    if value is not None:
        form.update(value["params"])
    form.update(updates)
    return form


def accepted(client, dialer, settings=SDK_SETTINGS):
    response = reserve(client)
    assert response.status_code == 202, response.text
    assert response.json()["audio_path"] == "native-conference"
    call = session_of(client, response.json()["session_id"])
    value = grant(client, call)
    response = signed_post(client, settings, "/twilio/browser-voice", app_form(call, value, settings))
    assert response.status_code == 200, response.text
    until(client, lambda: bool(call.legs[REMOTE].call_sid))
    return call, value, response


def conference_event(client, settings, call, role, *, event="participant-join", sequence=1):
    return signed_post(client, settings, f"/twilio/native-conference/{call.id}", {
        "AccountSid": settings.account_sid, "ConferenceSid": CONFERENCE,
        "FriendlyName": "phoney-" + call.id, "ParticipantLabel": role,
        "CallSid": call.legs[role].call_sid, "StatusCallbackEvent": event,
        "SequenceNumber": str(sequence)})


def test_sdk_browser_dials_approved_destination_only_after_signed_owner_binding_and_retry_is_idempotent():
    with sdk_client() as (client, dialer, settings):
        response = reserve(client)
        assert response.status_code == 202
        call = session_of(client, response.json()["session_id"])
        assert call.browser_audio and call.native_conference
        assert call.legs[OWNER].transport == "sdk"
        value = grant(client, call)
        settle(client)
        assert dialer.created == []
        assert value["params"]["Token"] != call.legs[OWNER].token
        form = app_form(call, value)
        first = signed_post(client, settings, "/twilio/browser-voice", form)
        assert first.status_code == 200
        until(client, lambda: bool(call.legs[REMOTE].call_sid))
        second = signed_post(client, settings, "/twilio/browser-voice", form)
        assert second.status_code == 200 and second.text == first.text
        settle(client)
        assert len(dialer.created) == 1 and dialer.created[0]["to"] == DESTINATION
        assert call.legs[OWNER].call_sid == OWNER_SID
        assert call.browser_voice_used and call.legs[OWNER].generation == 1
        assert not call.legs[OWNER].attached
        xml = ET.fromstring(first.text)
        assert xml.find("Start/Stream").attrib["track"] == "inbound_track"
        assert xml.find("Dial").attrib["action"].endswith("/twilio/browser-finished/" + call.id)
        assert "hangupOnStar" not in xml.find("Dial").attrib
        assert xml.find("Dial/Conference").attrib["endConferenceOnExit"] == "true"
        status_text = str(call.to_status()) + repr(call)
        assert value["params"]["Token"] not in status_text and value["token"] not in status_text
        assert client.post(f"/api/sessions/{call.id}/browser-token", headers=HEADERS).status_code == 409
        # A VoiceURL retry must not adopt a newer passive-observer identity.
        call.legs[OWNER].generation += 1
        call.legs[OWNER].token = "new-observer-identity"
        retry = signed_post(client, settings, "/twilio/browser-voice", form)
        assert retry.status_code == 200 and retry.text == first.text


@pytest.mark.parametrize("updates", [
    {"From": "client:someone_else"},
    {"From": "+12025550101"},
    {"Token": "incorrect"},
    {"Token": "non-ascii-\u2603"},
    {"CallSid": "CAinvalid"},
    {"ApplicationSid": "AP" + "4" * 32},
    {"ToAppSid": "AP" + "4" * 32},
    {"AccountSid": "AC" + "4" * 32},
])
def test_sdk_app_identity_nonce_application_and_account_are_required(updates):
    with sdk_client() as (client, dialer, settings):
        response = reserve(client)
        call = session_of(client, response.json()["session_id"])
        value = grant(client, call)
        response = signed_post(client, settings, "/twilio/browser-voice", app_form(call, value, **updates))
        assert response.status_code in {400, 403}
        assert not call.legs[OWNER].call_sid and not call.browser_voice_used
        assert dialer.created == [] and call.active


def test_sdk_app_requires_signature_and_nonce_is_rotated_bounded_and_single_call():
    with sdk_client() as (client, dialer, settings):
        response = reserve(client)
        call = session_of(client, response.json()["session_id"])
        old = grant(client, call)
        assert client.post("/twilio/browser-voice", data=app_form(call, old)).status_code == 403
        fresh = grant(client, call)
        assert fresh["params"]["Token"] != old["params"]["Token"]
        assert signed_post(client, settings, "/twilio/browser-voice", app_form(call, old)).status_code == 403
        call.browser_voice_issued -= TOKEN_SECONDS + 1
        assert signed_post(client, settings, "/twilio/browser-voice", app_form(call, fresh)).status_code == 403
        fresh = grant(client, call)
        assert signed_post(client, settings, "/twilio/browser-voice", app_form(call, fresh)).status_code == 200
        until(client, lambda: bool(call.legs[REMOTE].call_sid))
        assert signed_post(client, settings, "/twilio/browser-voice",
                           app_form(call, fresh, CallSid=OTHER_SID)).status_code == 403
        call.browser_voice_issued -= TOKEN_SECONDS + 1
        assert signed_post(client, settings, "/twilio/browser-voice", app_form(call, fresh)).status_code == 200
        settle(client)
        assert len(dialer.created) == 1 and call.legs[OWNER].call_sid == OWNER_SID


def test_unbound_sdk_status_and_conference_callbacks_cannot_claim_leg_or_dial():
    with sdk_client() as (client, dialer, settings):
        response = reserve(client)
        call = session_of(client, response.json()["session_id"])
        for path in ("/twilio/browser-status", f"/twilio/browser-status/{call.id}"):
            response = signed_post(client, settings, path, app_form(call, CallStatus="completed"))
            assert response.status_code == 204
        generic = signed_post(client, settings, f"/twilio/status/{call.id}/owner", {
            "AccountSid": settings.account_sid, "CallSid": OWNER_SID, "CallStatus": "in-progress"})
        assert generic.status_code == 400
        forged = signed_post(client, settings, f"/twilio/native-conference/{call.id}", {
            "AccountSid": settings.account_sid, "ConferenceSid": CONFERENCE,
            "FriendlyName": "phoney-" + call.id, "ParticipantLabel": OWNER,
            "CallSid": OWNER_SID, "StatusCallbackEvent": "participant-join", "SequenceNumber": "1"})
        assert forged.status_code == 400
        assert not call.legs[OWNER].call_sid and call.active and dialer.created == []


@pytest.mark.parametrize("pending_sid", [OWNER_SID, OTHER_SID])
def test_terminal_before_voice_webhook_only_retires_the_nonce_authenticated_call_sid(pending_sid):
    with sdk_client() as (client, dialer, settings):
        response = reserve(client)
        call = session_of(client, response.json()["session_id"])
        value = grant(client, call)
        early = signed_post(client, settings, "/twilio/browser-status",
            app_form(call, CallSid=pending_sid, CallStatus="completed"))
        assert early.status_code == 204 and not call.legs[OWNER].call_sid and call.active
        assert dialer.created == []
        response = signed_post(client, settings, "/twilio/browser-voice", app_form(call, value))
        assert response.status_code == 200
        settle(client)
        assert call.legs[OWNER].call_sid == OWNER_SID and call.browser_voice_used
        if pending_sid == OWNER_SID:
            assert not call.active and call.ended_reason == "owner-completed"
            assert dialer.created == [] and ET.fromstring(response.text).find("Hangup") is not None
        else:
            assert call.active and len(dialer.created) == 1


def test_native_browser_humans_connect_without_observers_and_owner_leave_ends_both_legs():
    with sdk_client() as (client, dialer, settings):
        call, _, _ = accepted(client, dialer)
        assert conference_event(client, settings, call, OWNER, sequence=1).status_code == 204
        assert conference_event(client, settings, call, REMOTE, sequence=2).status_code == 204
        assert call.phase == CONNECTED
        router = client.app.state.operator_controller.router(call.id)
        assert not router.attached(OWNER) and not router.attached(REMOTE)
        assert not call.legs[OWNER].attached and not call.legs[REMOTE].attached
        assert conference_event(client, settings, call, OWNER,
                                event="participant-leave", sequence=3).status_code == 204
        settle(client)
        assert call.phase == ENDED and call.ended_reason == "owner-left-conference"
        assert set(dialer.ended) == {OWNER_SID, REMOTE_SID}
        assert dialer.ended_conferences == [CONFERENCE]


@pytest.mark.parametrize("path", ["/twilio/browser-status", "/twilio/browser-status/{id}",
                                  "/twilio/browser-finished/{id}"])
def test_sdk_terminal_callbacks_cleanup_and_retries_never_redial(path):
    with sdk_client() as (client, dialer, settings):
        call, value, _ = accepted(client, dialer)
        path = path.format(id=call.id)
        form = app_form(call, CallStatus="completed", DialCallStatus="completed")
        response = signed_post(client, settings, path, form)
        assert response.status_code in {200, 204}
        settle(client)
        assert not call.active and REMOTE_SID in dialer.ended
        assert signed_post(client, settings, path, form).status_code in {200, 204}
        retry = signed_post(client, settings, "/twilio/browser-voice", app_form(call, value))
        assert retry.status_code == 200 and ET.fromstring(retry.text).find("Hangup") is not None
        settle(client)
        assert len(dialer.created) == 1 and dialer.ended.count(REMOTE_SID) == 1


def test_sdk_status_must_match_bound_identity_call_and_application():
    with sdk_client() as (client, dialer, settings):
        call, _, _ = accepted(client, dialer)
        for updates in ({"CallSid": OTHER_SID}, {"From": "client:someone_else"},
                        {"ToAppSid": "AP" + "4" * 32}):
            response = signed_post(client, settings, f"/twilio/browser-status/{call.id}",
                                   app_form(call, CallStatus="completed", **updates))
            assert response.status_code in {400, 403}
        assert call.active and dialer.ended == []


def test_owner_sdk_passive_observer_binds_twilio_sid_and_disconnect_preserves_human_call():
    with sdk_client() as (client, dialer, settings):
        call, _, _ = accepted(client, dialer)
        for index, role in enumerate((OWNER, REMOTE), 1):
            assert conference_event(client, settings, call, role, sequence=index).status_code == 204
        path = f"/conference-media/{call.id}/owner/"
        leg = call.legs[OWNER]
        with client.websocket_connect(path, headers=socket_headers(settings, path)) as socket:
            socket.send_json({"event": "start", "streamSid": OWNER_STREAM, "start": {
                "accountSid": settings.account_sid, "callSid": OWNER_SID, "streamSid": OWNER_STREAM,
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                "customParameters": {"generation": str(leg.generation), "token": leg.token}}})
            until(client, lambda: leg.attached)
            assert not client.app.state.operator_controller.router(call.id).channels[OWNER].attached
        until(client, lambda: not leg.attached)
        settle(client)
        assert call.phase == CONNECTED and dialer.ended == []


def test_sdk_sessions_reject_legacy_browser_phone_relay_and_phone_menu_paths():
    with sdk_client() as (client, dialer, settings):
        call, _, _ = accepted(client, dialer)
        for path, headers in ((f"/browser-media/{call.id}/", {"Origin": settings.public_base_url}),
                              (f"/media/{call.id}/remote/", socket_headers(settings, f"/media/{call.id}/remote/"))):
            with pytest.raises(WebSocketDisconnect) as rejected:
                with client.websocket_connect(path, headers=headers):
                    pass
            assert rejected.value.code == 1008
        for path in ("native-owner", "native-menu", "native-command"):
            response = signed_post(client, settings, f"/twilio/{path}/{call.id}", {
                "AccountSid": settings.account_sid, "CallSid": OWNER_SID, "Digits": "1",
                "DialCallStatus": "answered"})
            assert response.status_code == 400
        settle(client)
        assert call.active and len(dialer.created) == 1


def test_browser_request_respects_allowlist_public_policy_and_idempotent_transport():
    settings = replace(SDK_SETTINGS, public_calling_enabled=True)
    with sdk_client(settings) as (client, dialer, _):
        public = {"Origin": settings.public_base_url, "X-Agent-Request": "1"}
        assert reserve(client, headers=public).status_code == 403
        assert reserve(client, to="+442079460000").status_code == 403
        assert reserve(client, to=settings.twilio_number).status_code == 403
        key = str(uuid.uuid4())
        response = reserve(client, key=key)
        assert response.status_code == 202
        assert reserve(client, key=key).json()["duplicate"] is True
        assert start_call(client, key=key).status_code == 400
        assert reserve(client, key=key, to=OWNER_NUMBER).status_code == 400
        assert dialer.created == []
        public_config = client.get("/api/calls/config").json()
        assert "browser_voice_enabled" not in public_config and public_config["active_session"] is None
        owner_config = client.get("/api/calls/config", headers=HEADERS).json()
        assert owner_config["browser_voice_enabled"] is True


def test_flag_off_keeps_legacy_self_call_and_sdk_enabled_does_not_change_callback_defaults():
    with sdk_client(SETTINGS) as (client, dialer, _):
        assert reserve(client).status_code == 409
        response = start_call(client, to=OWNER_NUMBER)
        call = session_of(client, response.json()["session_id"])
        assert call.browser_audio and not call.native_conference and call.legs[OWNER].transport == "browser"
        value = client.post(f"/api/sessions/{call.id}/browser-token", headers=HEADERS).json()
        assert "url" in value and "transport" not in value and dialer.created == []
    with sdk_client() as (client, dialer, _):
        response = start_call(client)
        call = session_of(client, response.json()["session_id"])
        until(client, lambda: len(dialer.created) == 1)
        assert not call.browser_audio and not call.native_conference
        assert call.legs[OWNER].transport == "phone" and dialer.created[0]["to"] == OWNER_NUMBER


def test_sdk_hangup_before_remote_rest_returns_cleans_up_late_created_call():
    with sdk_client() as (client, dialer, settings):
        response = reserve(client)
        call = session_of(client, response.json()["session_id"])
        value = grant(client, call)
        dialer.hold = client.portal.call(asyncio.Event)
        response = signed_post(client, settings, "/twilio/browser-voice", app_form(call, value))
        assert response.status_code == 200
        until(client, lambda: len(dialer.created) == 1)
        assert call.legs[REMOTE].call_sid == ""
        response = signed_post(client, settings, "/twilio/browser-status", app_form(call, CallStatus="completed"))
        assert response.status_code == 204 and not call.active
        client.portal.call(dialer.hold.set)
        settle(client)
        assert dialer.ended.count(REMOTE_SID) == 1 and len(dialer.created) == 1


def test_workspace_cookie_authorizes_sdk_grant_and_signed_callbacks_need_no_browser_cookie(tmp_path):
    settings = replace(SDK_SETTINGS, workspace_access_enabled=True, agent_management_enabled=True,
                       workspace_storage_dir=str(tmp_path / "workspace"))
    with sdk_client(settings) as (client, dialer, _):
        access = client.app.state.workspace_access
        access.configure("642815", "test recovery phrase 924")
        _, _, token, _ = access.unlock(None, "testclient", "pin", "642815", True)
        client.cookies.set(SESSION_COOKIE, token)
        headers = {"Origin": settings.public_base_url, "X-Agent-Request": "1"}
        response = reserve(client, headers=headers)
        assert response.status_code == 202, response.text
        call = session_of(client, response.json()["session_id"])
        assert client.get("/api/calls/config").json()["browser_voice_enabled"] is True
        response = client.post(f"/api/sessions/{call.id}/browser-token", headers=headers)
        assert response.status_code == 200
        value = response.json()
        client.cookies.clear()
        assert client.post(f"/api/sessions/{call.id}/browser-token", headers=headers).status_code in {401, 403}
        response = signed_post(client, settings, "/twilio/browser-voice", app_form(call, value, settings))
        assert response.status_code == 200, response.text
        until(client, lambda: bool(call.legs[REMOTE].call_sid))
        assert signed_post(client, settings, "/twilio/browser-status",
                           app_form(call, settings=settings, CallStatus="completed")).status_code == 204
        settle(client)
        assert not call.active and REMOTE_SID in dialer.ended
