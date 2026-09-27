"""Full inbound app flow with local Twilio, Deepgram, Modulate and HTTP fakes.

These checks exercise real signed routes, sockets, registry revisions, audio
routing and storage. No transport in this module can place a call or reach a
provider.
"""

import asyncio
import base64
from contextlib import contextmanager, ExitStack
from dataclasses import replace
import json
from pathlib import Path
import time
import wave
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_registry import AgentSnapshot
from app import create_app
from media_capture.capture import decode_mulaw
from operator_service.runtime import ANNOUNCEMENT
from operator_service.sessions import AGENT, CONNECTED, ENDED, HUMAN, OWNER, REMOTE
from test_live_detection import SizedProviderSocket
from test_media_webhooks import Gateway, signed_post
from test_operator_routes import (
    ACCOUNT, DESTINATION, OWNER_FRAME, OWNER_NUMBER, REMOTE_FRAME, REMOTE_SID,
    REMOTE_STREAM, SETTINGS, Dialer, await_frame, connect,
    connected, keypad, media_message, next_event, settle, start_message,
    stream_parameter, until,
)
from test_transcription import Connector, result
from voice_stack.settings import VoiceSettings

AGENT_ID = "agent-admissions-test"
VOICE_ID = "integrationvoice1"
OTHER_VOICE_ID = "integrationvoice2"
PROMPT = "Answer admissions questions using the owner's published instructions."
REPLY = "Applications open next month."
AGENT_FRAME = b"\x2a" * 160
ANNOUNCEMENT_FRAME = b"\x3a" * 160


class Providers:
    """Only expected Gemini and ElevenLabs requests have an offline response."""

    def __init__(self):
        self.gemini = []
        self.speech = []
        self.detection = []

    def detect(self, url):
        socket = SizedProviderSocket()
        self.detection.append(socket)
        return socket

    def http(self, request):
        body = json.loads(request.content)
        if request.url.host == "generativelanguage.googleapis.com":
            assert request.method == "POST"
            assert request.url.path.endswith(":streamGenerateContent")
            self.gemini.append(body)
            event = {"candidates": [{"content": {"parts": [{"text": REPLY}]},
                                     "finishReason": "STOP"}]}
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  content="data: " + json.dumps(event) + "\n\n")
        assert request.url.host == "api.elevenlabs.io"
        assert request.method == "POST"
        assert request.url.params["output_format"] == "ulaw_8000"
        self.speech.append((request.url.path, body))
        frame = ANNOUNCEMENT_FRAME if body["text"] == ANNOUNCEMENT else AGENT_FRAME
        return httpx.Response(200, content=frame * 2)


@pytest.fixture
def manual_app(tmp_path):
    settings = replace(SETTINGS, callee_number=OWNER_NUMBER,
        voice_agent_enabled=True, agent_management_enabled=True, operator_inbound_enabled=True,
        media_capture_enabled=True, media_storage_dir=str(tmp_path / "audio"),
        transcription_enabled=True, deepgram_api_key="offline-deepgram-key",
        transcript_storage_dir=str(tmp_path / "transcripts"),
        workspace_storage_dir=str(tmp_path / "workspace"),
        call_details_storage_dir=str(tmp_path / "details"),
        modulate_detection_enabled=True, modulate_api_key="offline-modulate-key",
        detection_storage_dir=str(tmp_path / "detection"))
    voice = VoiceSettings(enabled=True, gemini_api_key="offline-gemini-key",
        elevenlabs_api_key="offline-elevenlabs-key", elevenlabs_voice_id=VOICE_ID,
        output_dir=str(tmp_path / "voice"))
    providers, stt, dialer = Providers(), Connector(), Dialer()
    app = create_app(settings, gateway=Gateway(), operator_dialer=dialer,
        operator_voice=voice, transcription_connector=stt, detection_connector=providers.detect)
    app.state.operator_controller.provider_transport = httpx.MockTransport(providers.http)
    registry = app.state.agent_registry
    for voice_id in (VOICE_ID, OTHER_VOICE_ID):
        registry.add_voice({"voiceId": voice_id, "name": voice_id,
                            "ready": True, "requiresVerification": False})
    registry.publish(AGENT_ID, {"name": "Admissions", "prompt": PROMPT,
        "voiceProfileId": "voice-" + VOICE_ID, "slot": 3})
    with TestClient(app, base_url=settings.public_base_url) as client:
        yield client, settings, dialer, stt, providers


def inbound(client, settings):
    return signed_post(client, settings, "/voice", {
        "AccountSid": ACCOUNT, "CallSid": REMOTE_SID, "From": DESTINATION,
        "To": settings.twilio_number, "Direction": "inbound"})


def remote_key(digit, sequence):
    return {**keypad(digit, sequence), "streamSid": REMOTE_STREAM}


@contextmanager
def inbound_sockets(client, settings, *, first=REMOTE):
    response = inbound(client, settings)
    assert response.status_code == 200
    settle(client)
    session, = client.app.state.operator.sessions.values()
    second = OWNER if first == REMOTE else REMOTE
    with ExitStack() as stack:
        sockets = {}
        for role in (first, second):
            socket = sockets[role] = stack.enter_context(connect(client, settings, session.id, role))
            socket.send_json(connected())
            socket.send_json(start_message(client, session.id, role))
            until(client, lambda: session.legs[role].attached)
        until(client, lambda: session.phase == CONNECTED)
        until(client, lambda: REMOTE_SID in client.app.state.bridge_pipeline.active_call_ids)
        yield session, sockets[OWNER], sockets[REMOTE]


def until_mark(client, remote, prefix):
    """Read real routed media, stopping before acknowledging the requested mark."""
    frames = []
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        event = next_event(client, remote, timeout=.25)
        if not event:
            continue
        if event["event"] == "media":
            frames.append(base64.b64decode(event["media"]["payload"]))
        if event["event"] == "mark" and event["mark"]["name"].startswith(prefix):
            return event, frames
    raise AssertionError("Expected playback mark did not reach the caller")


def acknowledge(remote, mark):
    remote.send_json({"event": "mark", "streamSid": REMOTE_STREAM, "mark": mark["mark"]})


def agent_segments(client):
    return [row for row in client.app.state.transcription.call_segments(REMOTE_SID)
            if row.get("source") == "agent"]


def terminal(client, settings, status="completed"):
    return signed_post(client, settings, "/status", {
        "AccountSid": ACCOUNT, "CallSid": REMOTE_SID,
        "CallStatus": status, "CallDuration": "37"})


@pytest.mark.parametrize("first_role", [REMOTE, OWNER])
def test_signed_inbound_joins_on_answer_without_keypad_or_ai(manual_app, first_role):
    client, settings, dialer, stt, providers = manual_app
    assert client.post("/voice", data={"CallSid": REMOTE_SID}).status_code == 403
    assert dialer.created == []
    first = inbound(client, settings)
    assert first.status_code == 200
    assert ET.fromstring(first.text).find("Dial/Conference") is None
    settle(client)
    session, = client.app.state.operator.sessions.values()
    assert session.direction == "inbound" and session.canonical_call_sid == REMOTE_SID
    assert session.legs[REMOTE].call_sid == REMOTE_SID
    assert stream_parameter(first.text, "token") == session.legs[REMOTE].token
    assert f"/media/{session.id}/remote/" in first.text
    assert ET.fromstring(first.text).find("Say").text == (
        "New College Data Science")
    assert ET.fromstring(dialer.created[0]["twiml"]).find("Say") is None
    repeated = inbound(client, settings)
    assert repeated.text == first.text
    settle(client)
    assert [call["to"] for call in dialer.created] == [OWNER_NUMBER]
    assert client.app.state.switchboard.sessions == {}

    with inbound_sockets(client, settings, first=first_role) as (session, owner, remote):
        remote.send_json(remote_key("#", 2))
        remote.send_json(remote_key("3", 3))
        router = client.app.state.operator_controller.router(session.id)
        until(client, lambda: router.counters["dtmf_ignored"] == 2)
        assert session.phase == CONNECTED and session.mode == HUMAN
        assert providers.gemini == providers.speech == []
        assert (session.id, "owner_accept") not in client.app.state.operator._timers
        assert len(dialer.created) == 1  # The incoming caller is never redialed.
        remote.send_json(remote_key("#", 4))
        remote.send_json(remote_key("3", 5))
        until(client, lambda: router.counters["dtmf_ignored"] == 4)
        assert session.mode == HUMAN and session.agent_snapshot is None
        assert providers.gemini == providers.speech == []
        owner.send_json(media_message(OWNER_FRAME, sequence="3"))
        await_frame(client, remote, OWNER_FRAME)
        remote.send_json(media_message(REMOTE_FRAME, role=REMOTE, sequence="6"))
        await_frame(client, owner, REMOTE_FRAME)
        assert terminal(client, settings).status_code == 204


@pytest.mark.parametrize("first_role", [REMOTE, OWNER])
def test_inbound_microphones_remain_private_until_both_streams_authenticate(manual_app, first_role):
    client, settings, dialer, stt, providers = manual_app
    assert inbound(client, settings).status_code == 200
    settle(client)
    session, = client.app.state.operator.sessions.values()
    second_role = OWNER if first_role == REMOTE else REMOTE
    frame = OWNER_FRAME if first_role == OWNER else REMOTE_FRAME
    with ExitStack() as stack:
        first = stack.enter_context(connect(client, settings, session.id, first_role))
        second = stack.enter_context(connect(client, settings, session.id, second_role))
        first.send_json(connected())
        first.send_json(start_message(client, session.id, first_role))
        second.send_json(connected())  # Not authenticated until its start payload.
        first.send_json(media_message(frame, role=first_role, sequence="2"))
        until(client, lambda: session.legs[first_role].counters["frames_in"] == 1)
        router = client.app.state.operator_controller.router(session.id)
        assert not router.channels[second_role].media
        assert router.counters["routed"] == 0
        assert session.phase != CONNECTED and session.mode == HUMAN
        assert providers.gemini == providers.speech == []
        second.send_json(start_message(client, session.id, second_role))
        until(client, lambda: session.phase == CONNECTED)
        owner, remote = (first, second) if first_role == OWNER else (second, first)
        owner.send_json(media_message(OWNER_FRAME, sequence="4"))
        remote.send_json(media_message(REMOTE_FRAME, role=REMOTE, sequence="3"))
        await_frame(client, remote, OWNER_FRAME)
        await_frame(client, owner, REMOTE_FRAME)
        assert providers.gemini == providers.speech == []
        assert terminal(client, settings).status_code == 204


def test_caller_hangup_ends_owner_call_returned_by_delayed_dial_response(manual_app):
    client, settings, dialer, stt, providers = manual_app
    dialer.hold = client.portal.call(asyncio.Event)
    try:
        assert inbound(client, settings).status_code == 200
        until(client, lambda: len(dialer.created) == 1)
        created_sid = dialer.created[0]["sid"]
        session, = client.app.state.operator.sessions.values()
        assert session.legs[OWNER].call_sid == ""
        assert terminal(client, settings).status_code == 204
        assert session.phase == ENDED and dialer.ended == []
    finally:
        # Release the fake even on assertion failure so shutdown cannot stall.
        client.portal.call(dialer.hold.set)
    settle(client)
    assert dialer.ended == [created_sid]
    assert client.app.state.operator.active_count == 0
    assert client.app.state.operator.pending_count == 0
    assert terminal(client, settings).status_code == 204
    settle(client)
    assert dialer.ended == [created_sid]


def test_manual_slot_uses_frozen_registry_snapshot_and_acknowledged_agent_provenance(manual_app):
    client, settings, dialer, stt, providers = manual_app
    with inbound_sockets(client, settings) as (session, owner, remote):
        owner.send_json(media_message(OWNER_FRAME, sequence="3"))
        await_frame(client, remote, OWNER_FRAME)
        remote.send_json(media_message(REMOTE_FRAME, role=REMOTE, sequence="2"))
        await_frame(client, owner, REMOTE_FRAME)
        until(client, lambda: len(stt.sockets) == 2 and len(providers.detection) == 1)
        client.portal.call(stt.sockets[0].push, result("When do applications open?"))
        client.portal.call(stt.sockets[1].push, result("Please help with admissions."))
        until(client, lambda: len(session.turns) == 2)
        assert providers.gemini == providers.speech == []

        owner.send_json(keypad("#", 4))
        owner.send_json(keypad("3", 5))
        until(client, lambda: session.agent_snapshot is not None)
        selected = session.agent_snapshot
        assert isinstance(selected, AgentSnapshot)
        assert selected.id == AGENT_ID and selected.slot == 3 and selected.revision == 1
        assert selected.voice_id == VOICE_ID and selected.prompt == PROMPT
        client.app.state.agent_registry.publish(AGENT_ID, {
            "name": "Updated agent", "prompt": "New instructions for future selections.",
            "voiceProfileId": "voice-" + OTHER_VOICE_ID, "slot": 3})

        announcement, cue_frames = until_mark(client, remote, "announcement-")
        assert ANNOUNCEMENT_FRAME in cue_frames
        assert agent_segments(client) == []
        acknowledge(remote, announcement)
        reply, spoken_frames = until_mark(client, remote, "reply-")
        assert AGENT_FRAME in spoken_frames and session.mode == AGENT
        assert agent_segments(client) == []  # Queued speech is not yet claimed as played.
        acknowledge(remote, reply)
        until(client, lambda: len(agent_segments(client)) == 1)
        assert agent_segments(client)[0]["speaker"] == "Admissions"
        assert agent_segments(client)[0]["text"] == REPLY
        assert agent_segments(client)[0]["delivery"] == "played"
        first_request = json.dumps(providers.gemini[0])
        assert PROMPT in first_request
        assert "remote: When do applications open?" in first_request
        assert "owner: Please help with admissions." in first_request

        # A later caller turn must reuse this call's immutable published revision.
        # Model actual later audio, not delayed STT from 20ms into the call.
        now_ms = client.app.state.operator_controller.elapsed_ms(session.id)
        later_ms = ((now_ms // 20) + 1) * 20
        # Twilio continues sending silence during agent speech. Preserve that
        # continuous remote timeline for the live detector as well as STT.
        for index, stamp in enumerate(range(20, later_ms + 1, 20), start=3):
            frame = REMOTE_FRAME if stamp == later_ms else b'\xff' * 160
            later_audio = media_message(frame, role=REMOTE, sequence=str(index))
            later_audio["media"]["timestamp"] = str(stamp)
            remote.send_json(later_audio)
        until(client, lambda: session.legs[REMOTE].counters["frames_in"] == later_ms // 20 + 1)
        client.portal.call(stt.sockets[0].push,
                           result("What should I prepare?", start=later_ms / 1000)
                           | {"speech_final": True})
        second_reply, frames = until_mark(client, remote, "reply-")
        assert AGENT_FRAME in frames
        acknowledge(remote, second_reply)
        until(client, lambda: len(agent_segments(client)) == 2)
        assert session.agent_snapshot is selected
        assert len(providers.gemini) == 2
        assert PROMPT in json.dumps(providers.gemini[1])
        assert "New instructions" not in json.dumps(providers.gemini[1])
        assert all(f"/{VOICE_ID}/stream" in path for path, _ in providers.speech)
        # Only the two caller speech frames are non-silent detector input.
        def detected_speech():
            return [item for item in providers.detection[0].sent
                    if isinstance(item, bytes) and any(item)]
        until(client, lambda: len(detected_speech()) == 2)
        assert detected_speech() == [
            decode_mulaw(REMOTE_FRAME), decode_mulaw(REMOTE_FRAME)]
        outbound_stt = b"".join(item for item in stt.sockets[1].sent if isinstance(item, bytes))
        assert OWNER_FRAME in outbound_stt
        assert AGENT_FRAME not in outbound_stt and ANNOUNCEMENT_FRAME not in outbound_stt

        # Manual release restores the microphone without generating a third reply.
        owner.send_json(keypad("#", 6))
        owner.send_json(keypad("0", 7))
        until(client, lambda: session.mode == HUMAN)
        owner.send_json(media_message(OWNER_FRAME, sequence="8"))
        await_frame(client, remote, OWNER_FRAME)
        assert len(providers.gemini) == 2
        assert terminal(client, settings).status_code == 204
        until(client, lambda: client.app.state.transcription.active_count == 0)
        persisted = json.loads((Path(settings.transcript_storage_dir) / f"{REMOTE_SID}.json").read_text())
        assert len([row for row in persisted["segments"] if row.get("source") == "agent"]) == 2
        with wave.open(str(Path(settings.media_storage_dir) / REMOTE_SID / "outbound.wav")) as wav:
            assert decode_mulaw(AGENT_FRAME) in wav.readframes(wav.getnframes())


@pytest.mark.parametrize("status", ["completed", "busy", "failed", "no-answer", "canceled"])
def test_signed_global_terminal_status_cleans_operator_and_pipeline(manual_app, status):
    client, settings, dialer, stt, providers = manual_app
    with inbound_sockets(client, settings) as (session, owner, remote):
        remote.send_json(media_message(REMOTE_FRAME, role=REMOTE))
        until(client, lambda: len(stt.sockets) == 2 and len(providers.detection) == 1)
        assert client.post("/status", data={"CallSid": REMOTE_SID,
                           "CallStatus": status}).status_code == 403
        assert session.phase == CONNECTED
        assert terminal(client, settings, status).status_code == 204
        until(client, lambda: session.phase == ENDED)
        until(client, lambda: not client.app.state.bridge_pipeline.active_call_ids)
        until(client, lambda: client.app.state.transcription.active_count == 0
              and client.app.state.live_detection.active_count == 0)
        settle(client)
        assert client.app.state.operator.active_count == 0
        assert client.app.state.media_capture.active_count == 0
        assert session.id not in client.app.state.operator_controller.routers
        assert all(socket.closed for socket in stt.sockets + providers.detection)
        details, = client.app.state.call_details.snapshot()["calls"]
        assert details["ended_at"] is not None and details["duration_seconds"] == 37
        assert details["call_sid"] == REMOTE_SID
        assert dialer.ended == [session.legs[OWNER].call_sid]
        assert terminal(client, settings, status).status_code == 204
        settle(client)
        assert dialer.ended == [session.legs[OWNER].call_sid]


def test_default_flags_keep_existing_passive_conference_flow(tmp_path):
    settings = replace(SETTINGS, callee_number=OWNER_NUMBER,
                       workspace_storage_dir=str(tmp_path / "workspace"))
    assert not settings.operator_inbound_enabled
    assert not settings.agent_management_enabled and not settings.voice_agent_enabled
    dialer, providers = Dialer(), Providers()
    with TestClient(create_app(settings, gateway=Gateway(), operator_dialer=dialer,
                              transcription_connector=Connector(),
                              detection_connector=providers.detect)) as client:
        response = inbound(client, settings)
        assert response.status_code == 200
        xml = ET.fromstring(response.text)
        assert xml.find("Connect") is None and xml.find("Start") is None
        assert xml.find("Dial/Conference").text == "operator-" + REMOTE_SID
        assert client.app.state.operator.sessions == {}
        assert client.app.state.bridge_pipeline.active_call_ids == set()
        assert len(client.app.state.switchboard.sessions) == 1
        assert dialer.created == [] and providers.detection == []
        assert terminal(client, settings).status_code == 204
        assert client.app.state.switchboard.active_count == 0
