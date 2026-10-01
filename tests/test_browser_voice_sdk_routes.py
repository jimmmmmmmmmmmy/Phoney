"""Focused product and boundary checks; test helpers live in support."""

from dataclasses import replace
import asyncio
import uuid
import xml.etree.ElementTree as ET

import pytest

from operator_service.sessions import CONNECTED, ENDED, OWNER, REMOTE, TOKEN_SECONDS
from workspace_auth.web import SESSION_COOKIE

from support.browser_voice_sdk_routes import CONFERENCE, OTHER_SID, SDK_SETTINGS, accepted, app_form, conference_event, grant, reserve, sdk_client
from support.media_webhooks import signed_post
from support.operator_routes import DESTINATION, HEADERS, OWNER_NUMBER, OWNER_SID, REMOTE_SID, SETTINGS, session_of, settle, start_call, until


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


def test_workspace_cookie_authorizes_sdk_grant_and_signed_callbacks_need_no_browser_cookie(tmp_path):
    settings = replace(SDK_SETTINGS, workspace_access_enabled=True, agent_management_enabled=True,
                       workspace_storage_dir=str(tmp_path / "workspace"),
                       call_details_storage_dir=str(tmp_path / "details"))
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
        quality_url = f"/api/calls/{REMOTE_SID}/quality"
        assert client.get(quality_url).status_code in {401, 403}
        assert client.get(quality_url + "/sent.wav").status_code in {401, 403}
        assert conference_event(client, settings, call, OWNER, sequence=1).status_code == 204
        assert conference_event(client, settings, call, REMOTE, sequence=2).status_code == 204
        settle(client)
        controller = client.app.state.operator_controller
        client.portal.call(controller.stream_started, call.id, REMOTE, "MZ" + "4" * 32)
        client.cookies.set(SESSION_COOKIE, token)
        payload = {"version": 1, "sequence": 1, "final": False, "elapsed_ms": 10000,
                   "codec": "opus", "device": {},
                   "samples": [{"elapsed_ms": 9000, "jitter": 12, "rtt": 70}], "warnings": []}
        assert client.post(f"/api/sessions/{call.id}/browser-quality", headers=headers,
                           json=payload).status_code == 204
        client.portal.call(controller.output_diagnostic, call.id, REMOTE,
                           {"event": "output-speech-stats", "role": REMOTE, "frames": 6})
        response = client.get(quality_url)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        quality = response.json()
        assert quality["browser"]["metrics"]["jitter"]["mean"] == 12
        assert quality["voice"]["events"][-1]["event"] == "audio-output-speech-stats"
        assert quality["voice"]["events"][-1]["frames"] == 6
        assert client.get(quality_url + "/unknown.wav").status_code == 404
        client.cookies.clear()
        assert signed_post(client, settings, "/twilio/browser-status",
                           app_form(call, settings=settings, CallStatus="completed")).status_code == 204
        settle(client)
        assert not call.active and REMOTE_SID in dialer.ended
