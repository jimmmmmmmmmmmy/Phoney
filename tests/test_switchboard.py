"""Signed HTTP integration checks; the fake gateway never places a phone call."""

import asyncio
import threading
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app import create_app
from config import Settings


PARENT = "CA" + "2" * 32
OUTBOUND = "CA" + "3" * 32
CONFERENCE = "CF" + "4" * 32
OTHER_CALL = "CA" + "5" * 32
OTHER_CONFERENCE = "CF" + "6" * 32
SETTINGS = Settings(
    account_sid="AC" + "1" * 32,
    auth_token="switchboard-test-secret",
    public_base_url="https://operator.example",
    twilio_number="+15555550200",
    callee_number="+15555550100",
)
VOICE = {
    "AccountSid": SETTINGS.account_sid,
    "CallSid": PARENT,
    "From": "+15555550300",
    "To": SETTINGS.twilio_number,
    "Direction": "inbound",
}


class FakeGateway:
    def __init__(self):
        self.created = []
        self.ended_calls = []
        self.ended_conferences = []
        self.lookups = []
        self.hold_create = False
        self.create_started = threading.Event()
        self.release_create = asyncio.Event()

    async def create_participant(self, conference_sid, parent_sid):
        self.created.append((conference_sid, parent_sid))
        self.create_started.set()
        if self.hold_create:
            await self.release_create.wait()
        return OUTBOUND

    async def find_participant(self, conference_sid, label="callee"):
        self.lookups.append((conference_sid, label))
        return None

    async def end_conference(self, conference_sid):
        self.ended_conferences.append(conference_sid)

    async def end_call(self, call_sid):
        self.ended_calls.append(call_sid)


@pytest.fixture
def switchboard():
    gateway = FakeGateway()
    with TestClient(create_app(SETTINGS, gateway=gateway)) as client:
        yield client, gateway
        # Also release a failed race test before the application's lifespan closes.
        client.portal.call(gateway.release_create.set)


def signed_post(client, path, form):
    signature = RequestValidator(SETTINGS.auth_token).compute_signature(
        SETTINGS.public_base_url + path, form
    )
    return client.post(path, data=form, headers={"X-Twilio-Signature": signature})


def conference_form(event="participant-join", **changes):
    form = {
        "AccountSid": SETTINGS.account_sid,
        "ConferenceSid": CONFERENCE,
        "FriendlyName": "operator-" + PARENT,
        "StatusCallbackEvent": event,
    }
    if event.startswith("participant-"):
        form.update(CallSid=PARENT, ParticipantLabel="caller")
    form.update(changes)
    return form


def call_form(status="ringing", **changes):
    return {
        "AccountSid": SETTINGS.account_sid,
        "CallSid": OUTBOUND,
        "CallStatus": status,
        "From": SETTINGS.twilio_number,
        "To": SETTINGS.callee_number,
        "Direction": "outbound-api",
        **changes,
    }


def drain(client):
    client.portal.call(client.app.state.switchboard.wait_idle)


def start_caller(client):
    assert signed_post(client, "/voice", VOICE).status_code == 200
    assert signed_post(client, f"/conference/events/{PARENT}", conference_form()).status_code == 204


def test_inbound_twiml_waits_for_teammate_and_binds_callbacks(switchboard):
    client, gateway = switchboard
    response = signed_post(client, "/voice", VOICE)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    xml = ET.fromstring(response.text)
    assert xml.find("Say").text == "New College Data Science Team"
    dial = xml.find("Dial")
    assert dial.attrib["action"] == SETTINGS.public_base_url + f"/conference/finished/{PARENT}"
    assert dial.attrib["method"] == "POST"
    conference = dial.find("Conference")
    assert conference.text == "operator-" + PARENT
    assert conference.attrib["participantLabel"] == "caller"
    assert conference.attrib["startConferenceOnEnter"] == "false"
    assert conference.attrib["endConferenceOnExit"] == "true"
    assert conference.attrib["maxParticipants"] == "2"
    assert conference.attrib["beep"] == "false"
    assert conference.attrib["statusCallback"] == SETTINGS.public_base_url + f"/conference/events/{PARENT}"
    assert conference.attrib["statusCallbackMethod"] == "POST"
    assert set(conference.attrib["statusCallbackEvent"].split()) == {"start", "end", "join", "leave"}
    drain(client)
    assert gateway.created == []


def test_health_identifies_build_and_readiness_without_call_data(switchboard):
    client, _ = switchboard
    assert client.get("/health").json() == {
        "status": "ok", "service": "passive-operator", "build": 2, "switchboard_ready": True,
        "media_capture_enabled": False
    }


def test_duplicate_voice_and_caller_join_create_only_one_outbound_leg(switchboard):
    client, gateway = switchboard
    start_caller(client)
    assert signed_post(client, "/voice", VOICE).status_code == 200
    for _ in range(3):
        assert signed_post(client, f"/conference/events/{PARENT}", conference_form()).status_code == 204
    drain(client)
    assert gateway.created == [(CONFERENCE, PARENT)]


def test_connected_requires_both_participants_and_conference_start(switchboard):
    client, gateway = switchboard
    start_caller(client)
    drain(client)
    session = client.app.state.switchboard.sessions[PARENT]
    assert session.phase == "dialing"
    assert signed_post(client, f"/calls/status/{PARENT}", call_form("in-progress")).status_code == 204
    assert session.phase == "dialing"
    assert signed_post(client, f"/conference/events/{PARENT}", conference_form(
        CallSid=OUTBOUND, ParticipantLabel="callee"
    )).status_code == 204
    assert session.phase == "dialing"
    # Conference-wide callbacks have no CallSid.
    assert signed_post(client, f"/conference/events/{PARENT}", conference_form("conference-start")).status_code == 204
    assert session.phase == "connected"
    assert gateway.created == [(CONFERENCE, PARENT)]


def test_conference_end_without_callsid_prevents_late_join_from_dialing(switchboard):
    client, gateway = switchboard
    assert signed_post(client, "/voice", VOICE).status_code == 200
    for event in ("conference-start", "conference-end", "participant-join"):
        assert signed_post(client, f"/conference/events/{PARENT}", conference_form(event)).status_code == 204
    drain(client)
    assert gateway.created == []
    assert client.app.state.switchboard.sessions[PARENT].phase == "ended"


@pytest.mark.parametrize("path,form", [
    (f"/conference/events/{PARENT}", conference_form()),
    (f"/calls/status/{PARENT}", call_form()),
    (f"/conference/finished/{PARENT}", VOICE),
])
def test_unsigned_and_wrong_account_callbacks_have_no_side_effects(switchboard, path, form):
    client, gateway = switchboard
    assert signed_post(client, "/voice", VOICE).status_code == 200
    assert client.post(path, data=form).status_code == 403
    assert signed_post(client, path, {**form, "AccountSid": "AC" + "9" * 32}).status_code == 403
    drain(client)
    assert gateway.created == []
    assert gateway.ended_calls == []
    assert gateway.ended_conferences == []
    assert client.app.state.switchboard.sessions[PARENT].phase == "waiting"


@pytest.mark.parametrize("changes", [
    {"FriendlyName": "operator-" + OTHER_CALL},
    {"CallSid": OTHER_CALL},
    {"ParticipantLabel": "unknown"},
])
def test_mismatched_caller_join_does_not_dial(switchboard, changes):
    client, gateway = switchboard
    assert signed_post(client, "/voice", VOICE).status_code == 200
    response = signed_post(client, f"/conference/events/{PARENT}", conference_form(**changes))
    assert response.status_code in {204, 400}
    drain(client)
    assert gateway.created == []
    assert client.app.state.switchboard.sessions[PARENT].phase == "waiting"


def test_callback_cannot_change_an_already_bound_conference(switchboard):
    client, gateway = switchboard
    assert signed_post(client, "/voice", VOICE).status_code == 200
    assert signed_post(client, f"/conference/events/{PARENT}", conference_form("conference-start")).status_code == 204
    response = signed_post(client, f"/conference/events/{PARENT}", conference_form(ConferenceSid=OTHER_CONFERENCE))
    assert response.status_code in {204, 400}
    drain(client)
    assert gateway.created == []
    assert client.app.state.switchboard.sessions[PARENT].conference_sid == CONFERENCE


def test_unrelated_outbound_status_cannot_end_a_call(switchboard):
    client, gateway = switchboard
    start_caller(client)
    drain(client)
    response = signed_post(client, f"/calls/status/{PARENT}", call_form("completed", CallSid=OTHER_CALL))
    assert response.status_code in {204, 400}
    drain(client)
    assert client.app.state.switchboard.sessions[PARENT].phase == "dialing"
    assert gateway.ended_calls == []
    assert gateway.ended_conferences == []


def test_finished_callback_cannot_end_a_different_call(switchboard):
    client, gateway = switchboard
    start_caller(client)
    drain(client)
    response = signed_post(client, f"/conference/finished/{PARENT}", {**VOICE, "CallSid": OTHER_CALL})
    assert response.status_code in {204, 400}
    drain(client)
    assert client.app.state.switchboard.sessions[PARENT].phase == "dialing"
    assert gateway.ended_calls == []
    assert gateway.ended_conferences == []


@pytest.mark.parametrize("path,form", [
    (f"/conference/events/{PARENT}", conference_form(ConferenceSid="not-a-conference")),
    (f"/conference/events/{PARENT}", conference_form(CallSid="not-a-call")),
    (f"/calls/status/{PARENT}", call_form(CallSid="not-a-call")),
    (f"/conference/finished/{PARENT}", {**VOICE, "CallSid": "not-a-call"}),
])
def test_malformed_callback_sids_are_rejected_before_state_changes(switchboard, path, form):
    client, gateway = switchboard
    assert signed_post(client, "/voice", VOICE).status_code == 200
    assert signed_post(client, path, form).status_code == 400
    drain(client)
    assert gateway.created == []
    assert gateway.ended_calls == []
    assert gateway.ended_conferences == []
    assert client.app.state.switchboard.sessions[PARENT].phase == "waiting"


def test_busy_callee_ends_session_and_late_voice_cannot_redial(switchboard):
    client, gateway = switchboard
    start_caller(client)
    drain(client)
    assert signed_post(client, f"/calls/status/{PARENT}", call_form("busy")).status_code == 204
    drain(client)
    assert client.app.state.switchboard.sessions[PARENT].phase == "ended"
    assert CONFERENCE in gateway.ended_conferences
    response = signed_post(client, "/voice", VOICE)
    assert ET.fromstring(response.text).find("Dial") is None
    assert signed_post(client, f"/conference/events/{PARENT}", conference_form()).status_code == 204
    drain(client)
    assert gateway.created == [(CONFERENCE, PARENT)]


def test_call_status_can_arrive_before_participant_create_returns(switchboard):
    client, gateway = switchboard
    gateway.hold_create = True
    start_caller(client)
    assert gateway.create_started.wait(1), "The outbound creation task was not scheduled"
    assert signed_post(client, f"/calls/status/{PARENT}", call_form("ringing")).status_code == 204
    client.portal.call(gateway.release_create.set)
    drain(client)
    session = client.app.state.switchboard.sessions[PARENT]
    assert session.outbound_sid == OUTBOUND
    assert session.phase == "dialing"
    assert gateway.created == [(CONFERENCE, PARENT)]


def test_caller_leaving_during_create_cancels_eventual_outbound_leg(switchboard):
    client, gateway = switchboard
    gateway.hold_create = True
    start_caller(client)
    assert gateway.create_started.wait(1), "The outbound creation task was not scheduled"
    assert signed_post(client, f"/conference/events/{PARENT}", conference_form("participant-leave")).status_code == 204
    assert client.app.state.switchboard.sessions[PARENT].phase == "ended"
    client.portal.call(gateway.release_create.set)
    drain(client)
    assert OUTBOUND in gateway.ended_calls
    assert gateway.created == [(CONFERENCE, PARENT)]


def test_request_supplied_destination_and_callback_are_never_used(switchboard):
    client, gateway = switchboard
    malicious = {
        **VOICE, "to": "+15555559999", "destination": "+15555559999",
        "CALLEE_NUMBER": "+15555559999", "StatusCallback": "https://attacker.example/callback",
    }
    response = signed_post(client, "/voice", malicious)
    assert response.status_code == 200
    assert "attacker.example" not in response.text
    assert "+15555559999" not in response.text
    assert signed_post(client, f"/conference/events/{PARENT}", conference_form()).status_code == 204
    drain(client)
    assert gateway.created == [(CONFERENCE, PARENT)]


def test_teammate_calling_the_twilio_number_cannot_ring_themself(switchboard):
    client, gateway = switchboard
    response = signed_post(client, "/voice", {**VOICE, "From": SETTINGS.callee_number})
    assert response.status_code == 200
    xml = ET.fromstring(response.text)
    assert xml.find("Dial") is None
    assert xml.find("Hangup") is not None
    drain(client)
    assert gateway.created == []


def test_inbound_request_for_a_different_number_is_rejected(switchboard):
    client, gateway = switchboard
    assert signed_post(client, "/voice", {**VOICE, "To": "+15555559999"}).status_code == 400
    drain(client)
    assert gateway.created == []


def test_finished_callback_is_idempotent_and_returns_hangup(switchboard):
    client, gateway = switchboard
    start_caller(client)
    drain(client)
    for _ in range(2):
        response = signed_post(client, f"/conference/finished/{PARENT}", {**VOICE, "DialCallStatus": "completed"})
        assert response.status_code == 200
        assert ET.fromstring(response.text).find("Hangup") is not None
    drain(client)
    assert client.app.state.switchboard.sessions[PARENT].phase == "ended"
    assert gateway.created == [(CONFERENCE, PARENT)]
