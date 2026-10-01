"""Native conference provider/XML contracts, using the real SDK without traffic."""

import asyncio
import json
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs
import xml.etree.ElementTree as ET

import pytest
from twilio.base.exceptions import TwilioRestException
from twilio.http.response import Response
from twilio.rest import Client

from operator_service.conference_gateway import (
    ACCEPT_PROMPT, NativeConferenceGatewayMixin, native_agent_twiml,
    native_leg_twiml, owner_accept_twiml, owner_menu_twiml,
)


ACCOUNT = "AC" + "a" * 32
APP = "AP" + "b" * 32
CONFERENCE = "CF" + "c" * 32
CALL = "CA" + "d" * 32
STREAM = "MZ" + "f" * 32
SESSION = "e" * 32
SETTINGS = SimpleNamespace(public_base_url="https://operator.example",
                           twilio_number="+12025550101",
                           twilio_conference_app_sid=APP)


class Transport:
    def __init__(self, *, failure=False, response=None):
        self.requests = []
        self.failure = failure
        self.response = response or {"call_sid": CALL, "conference_sid": CONFERENCE}

    def request(self, method, url, **kwargs):
        self.requests.append((threading.get_ident(), method, url, kwargs))
        if self.failure:
            return Response(503, json.dumps({"code": 20500, "message": "Unavailable"}))
        return Response(200, json.dumps(self.response))


class Gateway(NativeConferenceGatewayMixin):
    def __init__(self, transport):
        self.settings = SETTINGS
        self.client = Client(ACCOUNT, "offline-token", http_client=transport)

    def _client(self):
        return self.client


def test_agent_creation_uses_real_sdk_app_endpoint_and_callbacks_off_event_loop():
    transport = Transport()
    gateway = Gateway(transport)
    token = "encoded&token=still-one-value"
    main_thread = threading.get_ident()
    assert asyncio.run(gateway.create_agent_participant(CONFERENCE, SESSION, 2, token)) == CALL
    assert len(transport.requests) == 1
    thread, method, url, request = transport.requests[0]
    assert thread != main_thread
    assert method == "POST" and url.endswith(f"/Conferences/{CONFERENCE}/Participants.json")
    data = request["data"]
    destination, _, query = data["To"].partition("?")
    assert destination == "app:" + APP
    assert parse_qs(query) == {"session_id": [SESSION], "generation": ["2"], "token": [token]}
    assert data["From"] == SETTINGS.twilio_number
    assert data["Label"] == "agent" and data["Beep"] == "false"
    assert data["Muted"] == "true" and data["EarlyMedia"] == "false"
    assert data["EndConferenceOnExit"] == "false"
    assert data["StartConferenceOnEnter"] == "false"
    assert data["MaxParticipants"] == 3 and data["JitterBufferSize"] == "small"
    assert data["StatusCallback"] == SETTINGS.public_base_url + f"/twilio/native-agent/status/{SESSION}/2"
    assert data["StatusCallbackMethod"] == "POST"
    assert set(data["StatusCallbackEvent"]) == {"initiated", "ringing", "answered", "completed"}


def test_participant_controls_use_conference_and_call_identity():
    transport = Transport()
    gateway = Gateway(transport)
    async def run():
        await gateway.mute_participant(CONFERENCE, CALL, True)
        await gateway.mute_participant(CONFERENCE, CALL, False)
        await gateway.end_conference(CONFERENCE)
    asyncio.run(run())
    requests = transport.requests
    assert len(requests) == 3
    assert requests[0][2].endswith(f"/Conferences/{CONFERENCE}/Participants/{CALL}.json")
    assert requests[0][3]["data"] == {"Muted": "true"}
    assert requests[1][3]["data"] == {"Muted": "false"}
    assert requests[2][2].endswith(f"/Conferences/{CONFERENCE}.json")
    assert requests[2][3]["data"] == {"Status": "completed"}


def test_failed_creation_is_not_retried():
    transport = Transport(failure=True)
    with pytest.raises(TwilioRestException):
        asyncio.run(Gateway(transport).create_agent_participant(CONFERENCE, SESSION, 1, "token"))
    assert len(transport.requests) == 1


@pytest.mark.parametrize("role,track", [("owner", "inbound_track"), ("remote", "both_tracks")])
def test_observer_restart_creates_only_passive_stream_with_fresh_identity_off_event_loop(role, track):
    transport = Transport(response={"sid": STREAM, "call_sid": CALL})
    token = "new&token=still-one-value"
    main_thread = threading.get_ident()
    assert asyncio.run(Gateway(transport).restart_observer(CALL, SESSION, role, 2, token)) == STREAM
    assert len(transport.requests) == 1
    thread, method, url, request = transport.requests[0]
    assert thread != main_thread
    assert method == "POST" and url.endswith(f"/Calls/{CALL}/Streams.json")
    assert request["data"] == {
        "Url": f"wss://operator.example/conference-media/{SESSION}/{role}/",
        "Name": f"phoney-{role}-2", "Track": track,
        "Parameter1.Name": "generation", "Parameter1.Value": "2",
        "Parameter2.Name": "token", "Parameter2.Value": token,
    }


def test_failed_observer_restart_is_not_retried():
    transport = Transport(failure=True)
    with pytest.raises(TwilioRestException):
        asyncio.run(Gateway(transport).restart_observer(CALL, SESSION, "remote", 2, "token"))
    assert len(transport.requests) == 1


@pytest.mark.parametrize("call_sid,role,generation", [
    (CONFERENCE, "remote", 2), (CALL, "agent", 2), (CALL, "owner", True),
])
def test_invalid_observer_identity_cannot_place_provider_request(call_sid, role, generation):
    transport = Transport()
    with pytest.raises(ValueError):
        asyncio.run(Gateway(transport).restart_observer(call_sid, SESSION, role, generation, "token"))
    assert transport.requests == []


@pytest.mark.parametrize("role,track,starts", [
    ("remote", "both_tracks", "false"), ("owner", "inbound_track", "true"),
])
def test_human_tap_is_passive_and_humans_join_native_conference(role, track, starts):
    session = SimpleNamespace(id=SESSION, legs={role: SimpleNamespace(
        transport="phone", generation=3, token="auth&token")})
    response = ET.fromstring(native_leg_twiml(SETTINGS, session, role))
    assert [element.tag for element in response] == ["Start", "Dial"]
    assert response.find("Connect") is None
    stream = response.find("Start/Stream")
    assert stream.attrib == {"url": f"wss://operator.example/conference-media/{SESSION}/{role}/", "track": track}
    assert {p.attrib["name"]: p.attrib["value"] for p in stream} == {"generation": "3", "token": "auth&token"}
    conference = response.find("Dial/Conference")
    assert conference.text == "phoney-" + SESSION
    assert conference.attrib["participantLabel"] == role
    assert conference.attrib["startConferenceOnEnter"] == starts
    assert conference.attrib["endConferenceOnExit"] == ("true" if role == "remote" else "false")
    assert conference.attrib["muted"] == "false"
    assert conference.attrib["maxParticipants"] == "3"
    assert conference.attrib["beep"] == "false" and conference.attrib["jitterBufferSize"] == "small"
    assert conference.attrib["statusCallback"] == SETTINGS.public_base_url + f"/twilio/native-conference/{SESSION}"
    assert conference.attrib["statusCallbackMethod"] == "POST"
    assert {"start", "end", "join", "leave", "mute"} == set(conference.attrib["statusCallbackEvent"].split())
    expected_action = f"/twilio/native-menu/{SESSION}" if role == "owner" else f"/twilio/native-finished/{SESSION}/{role}"
    expected_dial = {"action": SETTINGS.public_base_url + expected_action, "method": "POST"}
    if role == "owner":
        expected_dial["hangupOnStar"] = "true"
    assert response.find("Dial").attrib == expected_dial


@pytest.mark.parametrize("mode", ["human", "preparing", "announcing", "agent"])
def test_owner_rejoins_muted_until_callback_reconciles_without_restarting_observer(mode):
    session = SimpleNamespace(id=SESSION, mode=mode,
                              legs={"owner": SimpleNamespace(transport="phone", generation=1, token="token")})
    response = ET.fromstring(native_leg_twiml(SETTINGS, session, "owner", rejoin=True))
    assert [element.tag for element in response] == ["Dial"]
    assert response.find("Dial/Conference").attrib["muted"] == "true"
    assert response.find("Dial/Conference").attrib["endConferenceOnExit"] == "false"


def test_owner_menu_dispatches_empty_result_so_timeout_rejoins_conference():
    response = ET.fromstring(owner_menu_twiml(SETTINGS, SimpleNamespace(id=SESSION)))
    assert [element.tag for element in response] == ["Gather"]
    gather = response.find("Gather")
    assert gather.attrib == {"input": "dtmf", "numDigits": "1", "timeout": "3",
                             "actionOnEmptyResult": "true", "method": "POST",
                             "action": SETTINGS.public_base_url + f"/twilio/native-command/{SESSION}"}
    assert gather.find("Say").text == "Press an agent number, or zero to resume speaking."


def test_owner_must_accept_before_joining_or_dialing_destination():
    response = ET.fromstring(owner_accept_twiml(SETTINGS, SimpleNamespace(id=SESSION)))
    assert [element.tag for element in response] == ["Gather", "Hangup"]
    gather = response.find("Gather")
    assert gather.attrib == {"numDigits": "1", "timeout": "20", "action": SETTINGS.public_base_url + f"/twilio/native-owner/{SESSION}", "method": "POST"}
    assert gather.find("Say").text == ACCEPT_PROMPT
    assert response.find("Dial") is None and response.find("Start") is None


def test_bot_stream_is_a_separate_bidirectional_call_with_auth_parameters():
    response = ET.fromstring(native_agent_twiml(SETTINGS, SESSION, 4, "auth&token"))
    assert [element.tag for element in response] == ["Connect", "Hangup"]
    assert response.find("Dial") is None and response.find("Start") is None
    stream = response.find("Connect/Stream")
    assert stream.attrib == {"url": f"wss://operator.example/native-agent-media/{SESSION}/"}
    assert {p.attrib["name"]: p.attrib["value"] for p in stream} == {"generation": "4", "token": "auth&token"}
    assert "?" not in stream.attrib["url"]


@pytest.mark.parametrize("generation", [0, -1, True, "1"])
def test_invalid_stream_generation_cannot_be_rendered_or_dialed(generation):
    transport = Transport()
    with pytest.raises(ValueError):
        native_agent_twiml(SETTINGS, SESSION, generation, "token")
    with pytest.raises(ValueError):
        asyncio.run(Gateway(transport).create_agent_participant(CONFERENCE, SESSION, generation, "token"))
    assert transport.requests == []


def test_rejects_unknown_human_role():
    with pytest.raises(ValueError, match="role"):
        native_leg_twiml(SETTINGS, SimpleNamespace(id=SESSION, legs={}), "agent")
