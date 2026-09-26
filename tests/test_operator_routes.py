"""Authenticated API, signed callbacks, and the two-leg bridge over fake sockets.

The Twilio REST surface is a local fake and the phone sockets are TestClient
websockets. Nothing here places a call.
"""

import asyncio
import base64
from contextlib import contextmanager
from dataclasses import replace
import json
import time
from unittest.mock import Mock
import xml.etree.ElementTree as ET
import uuid

import pytest
import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import create_app
from config import Settings
from operator_service.controls import load_profiles
from operator_service.sessions import CONNECTED, ENDED, HUMAN, OWNER, OWNER_PROMPT, REMOTE
from test_media_webhooks import Gateway, signed_post, socket_headers
from voice_stack.settings import VoiceSettings

ACCOUNT = "AC" + "a" * 32
OWNER_NUMBER = "+12025550101"
CALLEE = "+12025550102"
DESTINATION = "+12025550103"
ADMIN_TOKEN = "operator-admin-token-32-characters-long"
OWNER_SID = "CA" + "1" * 32
REMOTE_SID = "CA" + "2" * 32
OTHER_SID = "CA" + "5" * 32
OWNER_STREAM = "MZ" + "3" * 32
REMOTE_STREAM = "MZ" + "4" * 32
OWNER_FRAME = bytes([0x11]) * 160
REMOTE_FRAME = bytes([0x22]) * 160
CLIP_FRAME = bytes([0x2A]) * 160
VOICE_ID = "voiceid123"
SETTINGS = Settings(
    account_sid=ACCOUNT, auth_token="operator-route-test-token",
    public_base_url="https://operator.example", twilio_number=CALLEE,
    owner_number=OWNER_NUMBER, allowed_destinations=(DESTINATION,),
    operator_admin_token=ADMIN_TOKEN, max_call_seconds=1800,
    deploy_control_token="operator-deploy-control-token-32-chars",
)
HEADERS = {"Authorization": "Bearer " + ADMIN_TOKEN}
DEPLOY_HEADERS = {"Authorization": "Bearer " + SETTINGS.deploy_control_token}


class Dialer:
    """A fake Twilio REST surface: records legs, never places a call."""

    def __init__(self):
        self.created = []
        self.ended = []
        self.hold = None
        self.failure = None
        self.sid = None

    async def create_leg(self, *, to, twiml, status_callback):
        call_sid = self.sid or "CA" + format(len(self.created) + 1, "032x")
        self.created.append({"to": to, "twiml": twiml, "status_callback": status_callback,
                             "sid": call_sid})
        if self.hold is not None:
            await self.hold.wait()
        if self.failure is not None:
            raise self.failure
        return call_sid

    async def end_call(self, call_sid):
        self.ended.append(call_sid)


@contextmanager
def bridge_client(settings=None, voice=None):
    settings = settings or SETTINGS
    dialer = Dialer()
    with TestClient(create_app(settings, gateway=Gateway(), operator_dialer=dialer,
                               operator_voice=voice),
                    base_url=settings.public_base_url) as client:
        yield client, dialer, settings


def start_call(client, key=None, to=DESTINATION):
    return client.post("/api/calls/outbound", headers={**HEADERS, "Idempotency-Key": key or str(uuid.uuid4())},
                       json={"to": to, "goal": "Ask for an itemized out-of-the-door quote"})


def settle(client):
    client.portal.call(client.app.state.operator.wait_idle)


def until(client, predicate, timeout=2.0):
    async def wait():
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)

    client.portal.call(wait)


def session_of(client, session_id):
    return client.app.state.operator.sessions[session_id]


def next_event(client, socket, timeout=2.0):
    """One decoded server-sent Twilio event, or None if nothing arrived in time."""
    stream = getattr(socket, "_send_rx", None)
    if stream is None:
        return json.loads(socket.receive_text())

    async def bounded():
        try:
            return await asyncio.wait_for(stream.receive(), timeout)
        except Exception:
            return None

    try:
        message = client.portal.call(bounded)
    except Exception:
        return None
    if message is None or not isinstance(message.get("text"), str):
        return None
    try:
        return json.loads(message["text"])
    except ValueError:
        return None


def await_frame(client, socket, expected, timeout=2.0):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("the expected audio frame never arrived")
        message = next_event(client, socket, timeout=min(0.5, remaining))
        if message is None:
            continue
        if message.get("event") == "media":
            if base64.b64decode(message["media"]["payload"]) == expected:
                return message


def stream_parameter(twiml, name):
    stream = ET.fromstring(twiml).find("Connect/Stream")
    assert stream is not None
    return next(parameter.attrib["value"] for parameter in stream.findall("Parameter")
                if parameter.attrib["name"] == name)


def connected():
    return {"event": "connected", "protocol": "Call", "version": "1.0.0"}


def start_message(client, session_id, role, **changes):
    leg = session_of(client, session_id).legs[role]
    stream_sid = OWNER_STREAM if role == OWNER else REMOTE_STREAM
    start = {"accountSid": ACCOUNT, "callSid": leg.call_sid, "streamSid": stream_sid,
             "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
             "customParameters": {"generation": str(leg.generation), "token": leg.token}}
    start.update(changes)
    return {"event": "start", "sequenceNumber": "1", "streamSid": stream_sid, "start": start}


def media_message(frame, role=OWNER, sequence="2"):
    return {"event": "media", "sequenceNumber": sequence,
            "streamSid": OWNER_STREAM if role == OWNER else REMOTE_STREAM,
            "media": {"track": "inbound", "payload": base64.b64encode(frame).decode("ascii")}}


def keypad(digit, sequence):
    return {"event": "dtmf", "sequenceNumber": str(sequence), "streamSid": OWNER_STREAM,
            "dtmf": {"track": "inbound_track", "digit": digit}}


def connect(client, settings, session_id, role):
    path = f"/media/{session_id}/{role}/"
    return client.websocket_connect(path, headers=socket_headers(settings, path=path))


def accept_owner(client, settings, session_id, socket, **changes):
    socket.send_json(connected())
    socket.send_json(start_message(client, session_id, OWNER, **changes))
    until(client, lambda: session_of(client, session_id).phase == OWNER_PROMPT)


# ------------------------------------------------------------------- the API


def test_outbound_requires_the_admin_token_and_a_configured_bridge():
    with bridge_client() as (client, dialer, settings):
        assert client.post("/api/calls/outbound", json={"to": DESTINATION}).status_code == 403
        assert client.post("/api/calls/outbound", headers={"Authorization": "Bearer wrong"},
                           json={"to": DESTINATION}).status_code == 403
    unconfigured = replace(SETTINGS, owner_number="")
    with bridge_client(unconfigured) as (client, dialer, settings):
        response = client.post("/api/calls/outbound", headers=HEADERS, json={"to": DESTINATION})
        assert response.status_code == 503
        settle(client)
    assert dialer.created == []


def test_outbound_validates_the_payload_and_the_idempotency_key():
    with bridge_client() as (client, dialer, settings):
        assert client.post("/api/calls/outbound", headers=HEADERS,
                           json={"goal": "no destination"}).status_code == 400
        assert client.post("/api/calls/outbound", headers=HEADERS,
                           json={"to": DESTINATION, "extra": 1}).status_code == 400
        assert client.post("/api/calls/outbound", headers=HEADERS,
                           json={"to": 12025550103}).status_code == 400
        assert client.post("/api/calls/outbound", headers=HEADERS,
                           json=["to"]).status_code == 400
        assert client.post("/api/calls/outbound",
                           headers={**HEADERS, "Content-Type": "text/plain"},
                           content="nope").status_code == 400
        # The idempotency key is required and must be a UUID.
        assert client.post("/api/calls/outbound", headers=HEADERS,
                           json={"to": DESTINATION}).status_code == 400
        assert start_call(client, key="retry-1").status_code == 400             # not a UUID
        assert start_call(client, to=OWNER_NUMBER).status_code == 403           # not allowlisted
        assert start_call(client, to="+12025559999").status_code == 403
        settle(client)
        assert dialer.created == []
        # A goal is optional; the destination is not.
        accepted = client.post("/api/calls/outbound",
                               headers={**HEADERS, "Idempotency-Key": str(uuid.uuid4())},
                               json={"to": DESTINATION})
        assert accepted.status_code == 202
        assert session_of(client, accepted.json()["session_id"]).goal == ""
        settle(client)
        assert len(dialer.created) == 1


def test_a_duplicate_key_reuses_one_session_and_dials_once():
    with bridge_client() as (client, dialer, settings):
        key = str(uuid.uuid4())
        first = start_call(client, key=key)
        assert first.status_code == 202
        body = first.json()
        assert body["duplicate"] is False and body["phase"] == "reserved"
        assert body["status_url"] == f"/api/sessions/{body['session_id']}"
        settle(client)
        session_id = body["session_id"]
        assert len(dialer.created) == 1
        owner_leg = session_of(client, session_id).legs[OWNER]
        assert dialer.created[0]["to"] == OWNER_NUMBER
        assert dialer.created[0]["status_callback"].endswith(f"/twilio/status/{session_id}/owner")
        assert session_of(client, session_id).legs[OWNER].call_sid == dialer.created[0]["sid"]
        # The rejection path also has to refuse a second call for the same key.
        again = start_call(client, key=key)
        assert again.status_code == 202
        assert again.json()["duplicate"] is True
        assert again.json()["session_id"] == session_id
        settle(client)
        assert len(dialer.created) == 1
        assert start_call(client, key=str(uuid.uuid4())).status_code == 429        # capacity
        # The dialed instructions reserve the owner leg before the REST call.
        twiml = dialer.created[0]["twiml"]
        assert "Press 1 to connect." in twiml
        assert twiml.index("<Say") < twiml.index("<Connect")
        assert f'wss://operator.example/media/{session_id}/owner/' in twiml
        assert stream_parameter(twiml, "generation") == "1"
        assert stream_parameter(twiml, "token") == owner_leg.token
        assert f"/twilio/reconnect/{session_id}/owner" in twiml
        assert ADMIN_TOKEN not in twiml


def test_health_and_deploy_counts_include_the_bridge():
    with bridge_client() as (client, dialer, settings):
        assert client.get("/health").json()["operator_enabled"] is True
        assert client.get("/health").json()["media_capture_enabled"] is False
        session_id = start_call(client).json()["session_id"]
        settle(client)
        assert client.get("/internal/deploy").status_code == 403
        assert client.get("/internal/deploy", headers=DEPLOY_HEADERS).json() == {
            "draining": False, "active_sessions": 1, "pending_work": 0}
        draining = client.post("/internal/deploy", headers=DEPLOY_HEADERS, json={"draining": True})
        assert draining.json()["active_sessions"] == 1
        assert start_call(client).status_code == 503
        assert client.post(f"/api/sessions/{session_id}/end", headers=HEADERS).json()["phase"] == "ended"
        settle(client)
        assert client.post("/internal/deploy", headers=DEPLOY_HEADERS,
                           json={"draining": False}).json()["active_sessions"] == 0


@pytest.mark.parametrize("path,reason", [("unknown-session", "unknown"), ("role", "caller")])
def test_unsigned_or_unknown_stream_sockets_are_refused(path, reason):
    with bridge_client() as (client, dialer, settings):
        session_id = start_call(client).json()["session_id"]
        settle(client)
        target = f"/media/{'0' * 32 if path == 'unknown-session' else session_id}/{reason}/"
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(target):
                pytest.fail("an unsigned stream was accepted")
        assert rejected.value.code == 1008
        with pytest.raises(WebSocketDisconnect) as signed:
            with connect(client, settings, "0" * 32, reason):
                pytest.fail("an unknown session was accepted")
        assert signed.value.code == 1008
        assert session_of(client, session_id).legs[OWNER].attached is False


# --------------------------------------------------------------- the big flow


def test_owner_presses_one_and_both_people_hear_each_other():
    with bridge_client() as (client, dialer, settings):
        body = start_call(client).json()
        session_id = body["session_id"]
        settle(client)
        with connect(client, settings, session_id, OWNER) as owner:
            accept_owner(client, settings, session_id, owner)
            assert session_of(client, session_id).phase == OWNER_PROMPT
            owner.send_json(keypad("5", 2))         # only a bare 1 accepts
            settle(client)
            assert len(dialer.created) == 1
            owner.send_json(keypad("1", 3))
            until(client, lambda: len(dialer.created) == 2)
            assert session_of(client, session_id).phase == "remote_setup"
            assert len(dialer.created) == 2
            assert dialer.created[1]["to"] == DESTINATION
            assert "Press 1" not in dialer.created[1]["twiml"]
            assert f'wss://operator.example/media/{session_id}/remote/' in dialer.created[1]["twiml"]
            assert client.app.state.operator_controller.routers[session_id].cue is not None
            # A repeated command must not create a second remote call.
            owner.send_json(keypad("1", 4))
            until(client, lambda: session_of(client, session_id).legs[OWNER].counters["dtmf"] == 3)
            assert len(dialer.created) == 2
            with connect(client, settings, session_id, REMOTE) as remote:
                remote.send_json(connected())
                remote.send_json(start_message(client, session_id, REMOTE))
                until(client, lambda: session_of(client, session_id).phase == CONNECTED)
                assert client.app.state.operator_controller.routers[session_id].cue is None
                owner.send_json(media_message(OWNER_FRAME, sequence="5"))
                await_frame(client, remote, OWNER_FRAME)
                remote.send_json(media_message(REMOTE_FRAME, role=REMOTE, sequence="6"))
                await_frame(client, owner, REMOTE_FRAME)
                # The dealership's own keypad cannot change anything.
                remote.send_json({"event": "dtmf", "sequenceNumber": "7",
                                  "streamSid": REMOTE_STREAM,
                                  "dtmf": {"track": "inbound_track", "digit": "#1"}})
                settle(client)
                assert session_of(client, session_id).mode == HUMAN
                assert len(dialer.created) == 2
                owner_sid = session_of(client, session_id).legs[OWNER].call_sid
                remote_sid = session_of(client, session_id).legs[REMOTE].call_sid
                status = client.get(f"/api/sessions/{session_id}", headers=HEADERS).json()
                assert status["legs"][OWNER]["counters"]["dtmf"] == 3
                assert status["legs"][REMOTE]["counters"]["frames_in"] == 1
                body = client.get(f"/api/sessions/{session_id}", headers=HEADERS).text
                assert owner_sid not in body and remote_sid not in body
                ended = signed_post(client, settings, f"/twilio/status/{session_id}/owner",
                                    {"AccountSid": ACCOUNT, "CallSid": owner_sid,
                                     "CallStatus": "completed", "CallDuration": "62"})
                assert ended.status_code == 204
                settle(client)
                assert session_of(client, session_id).phase == ENDED
                # Only the surviving leg is hung up; the owner's call already ended.
                assert dialer.ended == [remote_sid]
                final = client.get(f"/api/sessions/{session_id}", headers=HEADERS).json()
                assert final["phase"] == ENDED and final["ended_reason"] == "owner-completed"
                assert final["legs"][OWNER]["call_status"] == "completed"
                # The already-closed session ignores later callbacks instead of reopening.
                assert signed_post(client, settings, f"/twilio/status/{session_id}/remote",
                                   {"AccountSid": ACCOUNT, "CallSid": remote_sid,
                                    "CallStatus": "completed"}).status_code == 204
                settle(client)
                assert dialer.ended == [remote_sid]


def test_a_signed_start_that_arrives_before_the_rest_result_still_binds():
    async def run_case(rest_sid, expected_reason=None):
        with bridge_client() as (client, dialer, settings):
            dialer.sid = rest_sid
            dialer.hold = asyncio.Event()
            body = start_call(client).json()
            session_id = body["session_id"]
            until(client, lambda: len(dialer.created) == 1)
            token = stream_parameter(dialer.created[0]["twiml"], "token")
            with connect(client, settings, session_id, OWNER) as owner:
                owner.send_json(connected())
                owner.send_json(start_message(client, session_id, OWNER,
                                              callSid=OTHER_SID,
                                              customParameters={"generation": "1", "token": token}))
                until(client, lambda: session_of(client, session_id).legs[OWNER].state == "started")
                assert session_of(client, session_id).legs[OWNER].call_sid == OTHER_SID
                client.portal.call(dialer.hold.set)
                settle(client)
                leg = session_of(client, session_id).legs[OWNER]
                if expected_reason is None:
                    assert leg.call_sid == OTHER_SID and session_of(client, session_id).phase == OWNER_PROMPT
                    assert dialer.ended == []
                else:
                    # The eventual REST result disagreed, so nothing is dialed twice.
                    assert session_of(client, session_id).phase == ENDED
                    assert session_of(client, session_id).ended_reason == expected_reason
                    assert dialer.ended == [OTHER_SID]

    asyncio.run(run_case(OTHER_SID))
    asyncio.run(run_case(OWNER_SID, expected_reason="owner-dial-mismatch"))


def test_an_uncertain_dial_is_reconciled_and_never_repeated():
    with bridge_client() as (client, dialer, settings):
        client.app.state.operator.deadlines["reconcile"] = 0.05
        dialer.failure = TimeoutError("fixture timeout")
        body = start_call(client).json()
        session_id = body["session_id"]
        settle(client)
        assert len(dialer.created) == 1
        assert session_of(client, session_id).legs[OWNER].uncertain is True
        until(client, lambda: session_of(client, session_id).phase == ENDED)
        assert session_of(client, session_id).ended_reason == "owner-dial-failed"
        settle(client)
        assert len(dialer.created) == 1        # no blind retry
        assert dialer.ended == []
        assert client.get(f"/api/sessions/{session_id}", headers=HEADERS).json()["phase"] == ENDED


def test_callbacks_and_recovery_check_session_role_and_sid():
    with bridge_client() as (client, dialer, settings):
        session_id = start_call(client).json()["session_id"]
        settle(client)
        owner_sid = session_of(client, session_id).legs[OWNER].call_sid
        assert client.post(f"/twilio/status/{session_id}/owner",
                           data={"CallSid": owner_sid}).status_code == 403
        assert signed_post(client, settings, f"/twilio/status/{session_id}/caller",
                           {"AccountSid": ACCOUNT, "CallSid": owner_sid,
                            "CallStatus": "ringing"}).status_code == 400
        assert signed_post(client, settings, f"/twilio/status/{session_id}/owner",
                           {"AccountSid": ACCOUNT, "CallSid": REMOTE_SID,
                            "CallStatus": "ringing"}).status_code == 400
        assert signed_post(client, settings, f"/twilio/status/{session_id}/owner",
                           {"AccountSid": ACCOUNT, "CallSid": "not-a-sid",
                            "CallStatus": "ringing"}).status_code == 400
        assert signed_post(client, settings, f"/twilio/status/{session_id}/owner",
                           {"AccountSid": ACCOUNT, "CallSid": owner_sid,
                            "CallStatus": "twirling"}).status_code == 400
        assert signed_post(client, settings, f"/twilio/status/{session_id}/owner",
                           {"AccountSid": ACCOUNT, "CallSid": owner_sid,
                            "CallStatus": "ringing"}).status_code == 204
        # Recovery before a stream has ever started cannot reuse the instructions.
        recovery = signed_post(client, settings, f"/twilio/reconnect/{session_id}/owner",
                               {"AccountSid": ACCOUNT, "CallSid": owner_sid})
        assert recovery.status_code == 200 and "<Hangup" in recovery.text
        assert session_of(client, session_id).phase == ENDED
        settle(client)
        assert dialer.ended == [owner_sid]


def test_recovery_rotates_the_stream_generation_and_is_rate_limited():
    with bridge_client() as (client, dialer, settings):
        session_id = start_call(client).json()["session_id"]
        settle(client)
        with connect(client, settings, session_id, OWNER) as owner:
            accept_owner(client, settings, session_id, owner)
            owner_sid = session_of(client, session_id).legs[OWNER].call_sid
            stale_token = session_of(client, session_id).legs[OWNER].token
            first = signed_post(client, settings, f"/twilio/reconnect/{session_id}/owner",
                                {"AccountSid": ACCOUNT, "CallSid": owner_sid})
            assert first.status_code == 200
            assert stream_parameter(first.text, "generation") == "2"
            assert stream_parameter(first.text, "token") != stale_token
            assert "Press 1" not in first.text
            # The other leg's SID can never take over this call.
            assert signed_post(client, settings, f"/twilio/reconnect/{session_id}/owner",
                               {"AccountSid": ACCOUNT, "CallSid": REMOTE_SID}).status_code == 400
            second = signed_post(client, settings, f"/twilio/reconnect/{session_id}/owner",
                                 {"AccountSid": ACCOUNT, "CallSid": owner_sid})
            assert stream_parameter(second.text, "generation") == "3"
            limited = signed_post(client, settings, f"/twilio/reconnect/{session_id}/owner",
                                  {"AccountSid": ACCOUNT, "CallSid": owner_sid})
            assert "<Hangup" in limited.text
            assert session_of(client, session_id).phase == ENDED
            settle(client)
            assert dialer.ended == [owner_sid]


class Speech:
    """A fake ElevenLabs endpoint: it records phrases and returns one clip."""

    def __init__(self, frame=CLIP_FRAME):
        self.frame = frame
        self.requests = []

    def transport(self):
        def handle(request):
            self.requests.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, content=self.frame * 2)

        return httpx.MockTransport(handle)


def test_the_owner_keypad_plays_the_cloned_phrase_and_returns_control(tmp_path):
    """The Stage 2 demo end to end: ``#1`` speaks, ``#0`` hands control back."""
    speech = Speech()
    voice = VoiceSettings(enabled=True, gemini_api_key="gemini-key",
                          elevenlabs_api_key="elevenlabs-key", elevenlabs_voice_id=VOICE_ID,
                          output_dir=str(tmp_path / "voice"))
    with bridge_client(voice=voice) as (client, dialer, settings):
        controller = client.app.state.operator_controller
        controller.clips.transport = speech.transport()
        session_id = start_call(client).json()["session_id"]
        settle(client)
        with connect(client, settings, session_id, OWNER) as owner:
            accept_owner(client, settings, session_id, owner)
            owner.send_json(keypad("1", 2))
            until(client, lambda: len(dialer.created) == 2)
            with connect(client, settings, session_id, REMOTE) as remote:
                remote.send_json(connected())
                remote.send_json(start_message(client, session_id, REMOTE))
                until(client, lambda: session_of(client, session_id).phase == CONNECTED)
                # The phrase is rendered once, while the two people are talking.
                until(client, lambda: not controller.prefetch)
                assert len(speech.requests) == 1
                owner.send_json(keypad("#", 3))
                owner.send_json(keypad("1", 4))
                until(client, lambda: session_of(client, session_id).mode == "agent")
                await_frame(client, remote, CLIP_FRAME)
                status = client.get(f"/api/sessions/{session_id}", headers=HEADERS).json()
                assert status["profile"] == "1" and status["reply_epoch"] == 1
                assert status["mode"] == "agent"
                assert speech.requests[0]["text"] == load_profiles()["1"].demo_phrase
                assert len(speech.requests) == 1        # the press used the cached clip
                # A remote keypad cannot select a profile, mid-takeover included.
                remote.send_json({"event": "dtmf", "sequenceNumber": "9",
                                  "streamSid": REMOTE_STREAM,
                                  "dtmf": {"track": "inbound_track", "digit": "#"}})
                settle(client)
                assert session_of(client, session_id).mode == "agent"
                assert len(dialer.created) == 2
                owner.send_json(keypad("#", 5))
                owner.send_json(keypad("0", 6))
                until(client, lambda: session_of(client, session_id).mode == "human")
                owner.send_json(media_message(OWNER_FRAME, sequence="7"))
                await_frame(client, remote, OWNER_FRAME)
                status = client.get(f"/api/sessions/{session_id}", headers=HEADERS).json()
                assert status["reply_epoch"] == 2
                settle(client)
                assert dialer.ended == []


def test_mode_and_end_routes_require_the_admin_token_and_stay_in_scope():
    with bridge_client() as (client, dialer, settings):
        session_id = start_call(client).json()["session_id"]
        settle(client)
        assert client.post(f"/api/sessions/{session_id}/end").status_code == 403
        assert client.get(f"/api/sessions/{session_id}").status_code == 403
        assert client.get(f"/api/sessions/{'0' * 32}", headers=HEADERS).status_code == 404
        assert client.post(f"/api/sessions/{session_id}/mode", headers=HEADERS,
                           json={"mode": "agent"}).json()["changed"] is True
        assert session_of(client, session_id).mode == "agent"
        assert session_of(client, session_id).reply_epoch == 1
        assert client.post(f"/api/sessions/{session_id}/mode", headers=HEADERS,
                           json={"mode": "agent"}).json()["changed"] is False
        assert client.post(f"/api/sessions/{session_id}/mode", headers=HEADERS,
                           json={"mode": "1", "extra": 1}).status_code == 400
        assert client.post(f"/api/sessions/{session_id}/mode", headers=HEADERS,
                           json={"mode": "human"}).json()["changed"] is True
        ended = client.post(f"/api/sessions/{session_id}/end", headers=HEADERS)
        assert ended.status_code == 200 and ended.json()["phase"] == ENDED
        owner_sid = dialer.created[0]["sid"]
        settle(client)
        assert dialer.ended == [owner_sid]
        assert client.post(f"/api/sessions/{session_id}/end", headers=HEADERS).json()["phase"] == ENDED
        settle(client)
        assert dialer.ended == [owner_sid]
        assert client.post(f"/api/sessions/{session_id}/mode", headers=HEADERS,
                           json={"mode": "agent"}).status_code == 409


# ------------------------------------------------------- the real REST contract


def test_the_leg_dialer_is_lazy_and_uses_bounded_rest_options(monkeypatch):
    from operator_service.routes import RING_TIMEOUT_SECONDS, TwilioLegs

    client = Mock()
    monkeypatch.setattr("operator_service.routes.Client", client)
    legs = TwilioLegs(SETTINGS)
    client.assert_not_called()      # importing or building the service never dials
    client.return_value.calls.create.return_value.sid = OWNER_SID
    call_sid = asyncio.run(legs.create_leg(
        to=DESTINATION, twiml="<Response/>",
        status_callback="https://operator.example/twilio/status/unknown/owner"))
    assert call_sid == OWNER_SID
    _, kwargs = client.return_value.calls.create.call_args
    assert kwargs["to"] == DESTINATION and kwargs["from_"] == CALLEE
    assert kwargs["twiml"] == "<Response/>"
    assert kwargs["timeout"] == RING_TIMEOUT_SECONDS == 25
    assert kwargs["time_limit"] == SETTINGS.max_call_seconds
    assert kwargs["status_callback_method"] == "POST"
    assert kwargs["status_callback_event"] == ["initiated", "ringing", "answered", "completed"]
    _, client_kwargs = client.call_args
    assert client_kwargs["account_sid"] == ACCOUNT
    assert client_kwargs["http_client"].timeout == 10
    assert client_kwargs["http_client"].session.get_adapter("https://").max_retries.total == 0


@pytest.mark.parametrize("status,update", [("queued", "canceled"), ("ringing", "canceled"),
                                          ("in-progress", "completed"), ("completed", None)])
def test_the_leg_dialer_ends_calls_in_their_current_state(monkeypatch, status, update):
    from operator_service.routes import TwilioLegs

    client = Mock()
    monkeypatch.setattr("operator_service.routes.Client", client)
    legs = TwilioLegs(SETTINGS)
    call = client.return_value.calls.return_value
    call.fetch.return_value.status = status
    asyncio.run(legs.end_call(OWNER_SID))
    if update is None:
        call.update.assert_not_called()
    else:
        call.update.assert_called_once_with(status=update)
