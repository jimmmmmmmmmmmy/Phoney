"""The owner's number uses browser audio and exactly one real telephone leg."""

import asyncio
import base64
from dataclasses import replace
import json
import uuid
import wave

import pytest
from starlette.websockets import WebSocketDisconnect

from test_operator_routes import (ACCOUNT, DESTINATION, HEADERS, OWNER_FRAME, OWNER_NUMBER,
                                  OWNER_SID, REMOTE_FRAME, REMOTE_STREAM, SETTINGS, await_frame, bridge_client, connect,
                                  media_message, next_event, session_of, settle, start_call,
                                  start_message, until)
from operator_service.sessions import CONNECTED, ENDED, OWNER, REMOTE, TOKEN_SECONDS
from media_capture.capture import decode_mulaw
from test_media_webhooks import signed_post
from workspace_auth.web import SESSION_COOKIE


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


def assert_rejected(client, grant, *, origin=SETTINGS.public_base_url, token=None):
    with pytest.raises(WebSocketDisconnect) as rejected:
        with browser_connect(client, grant, origin) as socket:
            socket.send_json({"event": "start", "token": token or grant["token"]})
            socket.receive_json()
    assert rejected.value.code == 1008


def test_owner_number_reserves_browser_call_and_dials_once_only_after_browser_ready():
    with bridge_client() as (client, dialer, _):
        key = str(uuid.uuid4())
        session_id = self_call(client, key)
        settle(client)
        assert dialer.created == []
        session = session_of(client, session_id)
        assert session.legs[OWNER].call_sid == ""
        grant = browser_token(client, session_id)
        settle(client)
        assert dialer.created == []
        with browser_connect(client, grant) as socket:
            browser_start(client, socket, grant)
            until(client, lambda: len(dialer.created) == 1)
            settle(client)
            assert dialer.created[0]["to"] == OWNER_NUMBER
            assert dialer.created[0]["status_callback"].endswith("/remote")
            assert session.legs[OWNER].call_sid == ""
            duplicate = start_call(client, key=key, to=OWNER_NUMBER)
            assert duplicate.status_code == 202
            assert duplicate.json()["session_id"] == session_id
            assert duplicate.json()["duplicate"] is True
            settle(client)
            assert len(dialer.created) == 1


def test_browser_and_owner_phone_exchange_audio_and_end_closes_only_real_call():
    with bridge_client() as (client, dialer, settings):
        session_id = self_call(client)
        grant = browser_token(client, session_id)
        with browser_connect(client, grant) as browser:
            browser_start(client, browser, grant)
            until(client, lambda: len(dialer.created) == 1)
            settle(client)
            with connect(client, settings, session_id, REMOTE) as phone:
                phone.send_json(start_message(client, session_id, REMOTE))
                until(client, lambda: session_of(client, session_id).phase == CONNECTED)
                browser.send_json({"event": "media", "media": {
                    "track": "inbound", "payload": base64.b64encode(OWNER_FRAME).decode(),
                    "timestamp": "0"}})
                await_frame(client, phone, OWNER_FRAME)
                phone.send_json(media_message(REMOTE_FRAME, role=REMOTE))
                await_frame(client, browser, REMOTE_FRAME)
                response = client.post(f"/api/sessions/{session_id}/end", headers=HEADERS)
                assert response.status_code == 200
                settle(client)
                assert session_of(client, session_id).phase == ENDED
                assert dialer.ended == [dialer.created[0]["sid"]]
                assert not session_of(client, session_id).legs[OWNER].attached


def test_browser_self_call_saves_both_audio_directions_under_real_remote_call_sid(tmp_path):
    settings = replace(SETTINGS, media_capture_enabled=True,
                       media_storage_dir=str(tmp_path / "captures"))
    with bridge_client(settings) as (client, dialer, _):
        session_id = self_call(client)
        grant = browser_token(client, session_id)
        with browser_connect(client, grant) as browser:
            browser_start(client, browser, grant)
            until(client, lambda: len(dialer.created) == 1)
            settle(client)
            call_sid = dialer.created[0]["sid"]
            with connect(client, settings, session_id, REMOTE) as phone:
                phone.send_json(start_message(client, session_id, REMOTE))
                until(client, lambda: session_of(client, session_id).phase == CONNECTED)
                session = session_of(client, session_id)
                assert session.canonical_call_sid == call_sid
                assert session.legs[OWNER].call_sid == ""
                for index in range(3):
                    browser.send_json({"event": "media", "media": {
                        "track": "inbound", "timestamp": str(index * 20),
                        "payload": base64.b64encode(OWNER_FRAME).decode()}})
                    await_frame(client, phone, OWNER_FRAME)
                    phone.send_json(media_message(REMOTE_FRAME, role=REMOTE, sequence=str(index + 2)))
                    await_frame(client, browser, REMOTE_FRAME)
                response = client.post(f"/api/sessions/{session_id}/end", headers=HEADERS)
                assert response.status_code == 200
                settle(client)

        capture_dir = tmp_path / "captures" / call_sid
        manifest_path = capture_dir / "manifest.json"
        until(client, manifest_path.exists)
        manifest = json.loads(manifest_path.read_text())
        assert manifest["status"] == "completed"
        assert manifest["finish_reason"] == "call-ended"
        assert manifest["call_sid"] == call_sid
        assert manifest["stream_sid"] == REMOTE_STREAM
        assert manifest["sources"] == [{"role": REMOTE, "stream_sid": REMOTE_STREAM,
                                        "generation": 1}]
        for track, frame in (("inbound", REMOTE_FRAME), ("outbound", OWNER_FRAME)):
            with wave.open(str(capture_dir / f"{track}.wav"), "rb") as wav:
                assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 8000)
                assert wav.getnframes() >= 3 * 160
                assert wav.readframes(wav.getnframes()).count(decode_mulaw(frame)) == 3
            assert manifest["tracks"][track]["frames"] >= 3
        assert session.canonical_call_sid == call_sid
        assert client.app.state.media_capture.active_count == 0
        assert client.app.state.transcription.active_count == 0
        catalog = client.get("/api/recordings").json()["recordings"]
        assert len(catalog) == 1 and catalog[0]["call_sid"] == call_sid
        assert catalog[0]["status"] == "completed"
        assert client.get(catalog[0]["url"]).status_code == 200


@pytest.mark.parametrize("origin", [None, "https://attacker.example", "null"])
def test_browser_audio_requires_same_origin_and_does_not_dial_on_rejection(origin):
    with bridge_client() as (client, dialer, _):
        session_id = self_call(client)
        grant = browser_token(client, session_id)
        assert_rejected(client, grant, origin=origin)
        settle(client)
        assert dialer.created == []
        assert session_of(client, session_id).active


def test_browser_token_is_owner_authorized_and_cannot_be_used_for_regular_phone_call():
    with bridge_client() as (client, dialer, _):
        session_id = self_call(client)
        path = f"/api/sessions/{session_id}/browser-token"
        assert client.post(path).status_code == 403
        assert client.post(path, headers={"Authorization": "Bearer wrong"}).status_code == 403
        assert dialer.created == []
        client.post(f"/api/sessions/{session_id}/end", headers=HEADERS)
        normal = start_call(client, to=DESTINATION)
        assert normal.status_code == 202
        assert normal.json()["browser_audio"] is False
        assert client.post(f"/api/sessions/{normal.json()['session_id']}/browser-token",
                           headers=HEADERS).status_code == 409


def test_invalid_token_and_duplicate_browser_cannot_detach_live_browser_or_redial():
    with bridge_client() as (client, dialer, _):
        session_id = self_call(client)
        grant = browser_token(client, session_id)
        assert_rejected(client, grant, token="invalid-token")
        assert session_of(client, session_id).active
        assert dialer.created == []
        with browser_connect(client, grant) as browser:
            browser_start(client, browser, grant)
            until(client, lambda: len(dialer.created) == 1)
            assert client.post(f"/api/sessions/{session_id}/browser-token",
                               headers=HEADERS).status_code == 409
            assert_rejected(client, grant)
            assert session_of(client, session_id).active
            assert session_of(client, session_id).legs[OWNER].attached
            assert client.app.state.operator_controller.router(session_id).attached(OWNER)
            settle(client)
            assert len(dialer.created) == 1


def test_expired_browser_token_does_not_dial_and_new_owner_grant_can_connect():
    with bridge_client() as (client, dialer, _):
        session_id = self_call(client)
        expired = browser_token(client, session_id)
        session_of(client, session_id).legs[OWNER].token_issued -= TOKEN_SECONDS + 1
        assert_rejected(client, expired)
        assert dialer.created == []
        assert session_of(client, session_id).active
        fresh = browser_token(client, session_id)
        assert fresh["token"] != expired["token"]
        assert_rejected(client, expired)
        with browser_connect(client, fresh) as browser:
            browser_start(client, browser, fresh)
            until(client, lambda: len(dialer.created) == 1)


def test_browser_audio_requires_start_before_media_and_never_accepts_url_credentials():
    with bridge_client() as (client, dialer, _):
        session_id = self_call(client)
        grant = browser_token(client, session_id)
        assert_rejected(client, {**grant, "url": grant["url"] + "?token=" + grant["token"]})
        with pytest.raises(WebSocketDisconnect) as rejected:
            with browser_connect(client, grant) as browser:
                browser.send_json({"event": "media", "media": {"payload": "AAAA"}})
                browser.receive_json()
        assert rejected.value.code == 1008
        assert session_of(client, session_id).active
        assert dialer.created == []


def test_malformed_authenticated_audio_closes_browser_and_its_single_phone_leg():
    with bridge_client() as (client, dialer, _):
        session_id = self_call(client)
        grant = browser_token(client, session_id)
        with browser_connect(client, grant) as browser:
            browser_start(client, browser, grant)
            until(client, lambda: len(dialer.created) == 1)
            settle(client)
            for _ in range(10):
                browser.send_json({"event": "media", "media": {
                    "track": "inbound", "payload": "!!!!"}})
            until(client, lambda: session_of(client, session_id).phase == ENDED)
            settle(client)
            assert dialer.ended == [dialer.created[0]["sid"]]
            assert not session_of(client, session_id).legs[OWNER].attached


def test_signed_twilio_owner_events_cannot_bind_or_end_a_browser_leg():
    with bridge_client() as (client, dialer, settings):
        session_id = self_call(client)
        grant = browser_token(client, session_id)
        with browser_connect(client, grant) as browser:
            browser_start(client, browser, grant)
            until(client, lambda: len(dialer.created) == 1)
            with pytest.raises(WebSocketDisconnect) as rejected:
                with connect(client, settings, session_id, OWNER):
                    pass
            assert rejected.value.code == 1008
            form = {"AccountSid": ACCOUNT, "CallSid": OWNER_SID, "CallStatus": "completed"}
            assert signed_post(client, settings,
                               f"/twilio/status/{session_id}/owner", form).status_code == 400
            assert signed_post(client, settings,
                               f"/twilio/reconnect/{session_id}/owner", form).status_code == 400
            assert session_of(client, session_id).active
            assert session_of(client, session_id).legs[OWNER].call_sid == ""
            assert session_of(client, session_id).legs[OWNER].attached
            assert client.app.state.operator_controller.router(session_id).attached(OWNER)


def test_browser_disconnect_ends_phone_call_and_consumed_token_cannot_reconnect():
    with bridge_client() as (client, dialer, _):
        session_id = self_call(client)
        grant = browser_token(client, session_id)
        with browser_connect(client, grant) as browser:
            browser_start(client, browser, grant)
            until(client, lambda: len(dialer.created) == 1)
            settle(client)
        until(client, lambda: session_of(client, session_id).phase == ENDED)
        settle(client)
        assert dialer.ended == [dialer.created[0]["sid"]]
        assert_rejected(client, grant)
        assert client.post(f"/api/sessions/{session_id}/browser-token",
                           headers=HEADERS).status_code == 409
        assert len(dialer.created) == 1


def test_browser_disconnect_during_dial_ends_late_rest_result_without_redialing():
    with bridge_client() as (client, dialer, _):
        dialer.hold = client.portal.call(asyncio.Event)
        session_id = self_call(client)
        grant = browser_token(client, session_id)
        with browser_connect(client, grant) as browser:
            browser_start(client, browser, grant)
            until(client, lambda: len(dialer.created) == 1)
        until(client, lambda: session_of(client, session_id).phase == ENDED)
        assert dialer.ended == []  # REST has not returned the phone call SID yet.
        client.portal.call(dialer.hold.set)
        settle(client)
        assert dialer.ended == [dialer.created[0]["sid"]]
        assert len(dialer.created) == 1


def test_remote_terminal_callback_closes_browser_without_an_extra_phone_hangup():
    with bridge_client() as (client, dialer, settings):
        session_id = self_call(client)
        grant = browser_token(client, session_id)
        with browser_connect(client, grant) as browser:
            browser_start(client, browser, grant)
            until(client, lambda: len(dialer.created) == 1)
            settle(client)
            response = signed_post(client, settings, f"/twilio/status/{session_id}/remote", {
                "AccountSid": ACCOUNT, "CallSid": dialer.created[0]["sid"],
                "CallStatus": "completed"})
            assert response.status_code == 204
            settle(client)
            assert session_of(client, session_id).phase == ENDED
            assert not session_of(client, session_id).legs[OWNER].attached
            assert dialer.ended == []
            with pytest.raises(WebSocketDisconnect) as ended:
                # Drain any ringback already sent before the hangup callback.
                for _ in range(100):
                    browser.receive_json()
            assert ended.value.code == 1000


def test_browser_call_config_exposes_self_call_number_only_to_authorized_owner():
    with bridge_client() as (client, _, _):
        public = client.get("/api/calls/config").json()
        assert public.get("self_call_number") in {None, ""}
        owner = client.get("/api/calls/config", headers=HEADERS).json()
        assert owner["self_call_number"] == OWNER_NUMBER
        assert owner["destinations"] == [DESTINATION]


def test_public_calling_keeps_us_policy_but_cannot_start_or_control_owner_browser_call():
    settings = replace(SETTINGS, public_calling_enabled=True, allowed_destinations=(),
                       allowed_destination_countries=("US",))
    with bridge_client(settings) as (client, dialer, _):
        public_headers = {"Origin": settings.public_base_url, "X-Agent-Request": "1",
                          "Idempotency-Key": str(uuid.uuid4())}
        public_config = client.get("/api/calls/config").json()
        assert public_config["public_calling"] is True
        assert public_config["countries"] == ["US"]
        assert public_config.get("self_call_number") in {None, ""}
        response = client.post("/api/calls/outbound", headers=public_headers,
                               json={"to": OWNER_NUMBER, "goal": ""})
        assert response.status_code == 403
        assert dialer.created == []
        session_id = self_call(client)
        assert client.get("/api/calls/config").json()["active_session"] is None
        assert client.get(f"/api/sessions/{session_id}").status_code == 403
        assert client.post(f"/api/sessions/{session_id}/end", headers=public_headers).status_code == 403
        assert client.post(f"/api/sessions/{session_id}/browser-token",
                           headers=public_headers).status_code == 403
        assert session_of(client, session_id).active
        assert dialer.created == []


def test_workspace_gate_requires_cookie_and_origin_for_browser_audio(tmp_path):
    settings = replace(SETTINGS, workspace_access_enabled=True,
                       workspace_storage_dir=str(tmp_path / "workspace"))
    with bridge_client(settings) as (client, dialer, _):
        access = client.app.state.workspace_access
        access.configure("642815", "test recovery phrase 924")
        _, _, token, _ = access.unlock(None, "testclient", "pin", "642815", True)
        client.cookies.set(SESSION_COOKIE, token)
        session_id = self_call(client)
        grant = browser_token(client, session_id)
        client.cookies.clear()
        assert_rejected(client, grant)
        client.cookies.set(SESSION_COOKIE, "invalid")
        assert_rejected(client, grant)
        client.cookies.set(SESSION_COOKIE, token)
        assert_rejected(client, grant, origin="https://attacker.example")
        with browser_connect(client, grant) as browser:
            browser_start(client, browser, grant)
            until(client, lambda: len(dialer.created) == 1)
            assert session_of(client, session_id).legs[OWNER].attached


def test_service_number_stays_rejected_without_dialing():
    with bridge_client() as (client, dialer, settings):
        response = start_call(client, to=settings.twilio_number)
        assert response.status_code == 403
        assert dialer.created == []


def test_shared_workspace_cookie_authorizes_self_call_without_admin_or_owner_token(tmp_path):
    settings = replace(SETTINGS, workspace_access_enabled=True, agent_management_enabled=True,
                       workspace_storage_dir=str(tmp_path / "workspace"))
    with bridge_client(settings) as (client, dialer, _):
        access = client.app.state.workspace_access
        access.configure("642815", "test recovery phrase 924")
        _, _, token, _ = access.unlock(None, "testclient", "pin", "642815", True)
        client.cookies.set(SESSION_COOKIE, token)
        config = client.get("/api/calls/config").json()
        assert config["authenticated"] is True and config["self_call_number"] == OWNER_NUMBER
        headers = {"Origin": settings.public_base_url, "X-Agent-Request": "1"}
        started = client.post("/api/calls/outbound", json={"to": OWNER_NUMBER},
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())})
        assert started.status_code == 202
        session_id = started.json()["session_id"]
        grant = browser_token(client, session_id, headers=headers)
        with browser_connect(client, grant) as browser:
            browser_start(client, browser, grant)
            until(client, lambda: len(dialer.created) == 1)
            assert dialer.created[0]["to"] == OWNER_NUMBER
