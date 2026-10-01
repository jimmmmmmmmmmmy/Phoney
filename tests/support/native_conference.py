"""Shared fixtures and fakes for focused integration checks."""

import asyncio


import base64


from dataclasses import replace


import json


import uuid


import pytest


from twilio.base.exceptions import TwilioRestException


from operator_service import OperatorSessions


from operator_service.native_conference import NativeConferenceRouter


from operator_service.routes import OperatorController


from operator_service.sessions import AGENT, ANNOUNCING, CONNECTED, HUMAN, OWNER, PREPARING, REMOTE


from support.operator_keypad import (
    Provider,
    Registry,
    SETTINGS,
    Socket,
    OWNER_SID,
    REMOTE_SID,
    OWNER_STREAM,
    REMOTE_STREAM,
    until,
)


from voice_stack.settings import VoiceSettings


BOT_SID = "CA" + "5" * 32


BOT_STREAM = "MZ" + "6" * 32


CONFERENCE = "CF" + "7" * 32


DESTINATION = "+12025550103"


NATIVE_SETTINGS = replace(SETTINGS, native_conference_enabled=True,
                          twilio_conference_app_sid="AP" + "8" * 32)


class NativeDialer:
    def __init__(self):
        self.created, self.operations, self.ended, self.ended_conferences = [], [], [], []
        self.controller = None
        self.bot_hold = None
        self.bot_entered = asyncio.Event()
        self.fail_owner_mute = False
        self.owner_mute_hold = None
        self.owner_mute_entered = asyncio.Event()
        self.fail_bot_mute_404 = False
        self.actual_muted = {OWNER_SID: False, BOT_SID: True}

    async def create_leg(self, **kwargs):
        self.created.append(kwargs)
        return OWNER_SID if len(self.created) == 1 else REMOTE_SID

    async def create_agent_participant(self, conference_sid, session_id, generation, token):
        self.operations.append(("create-agent", conference_sid, generation))
        self.bot_entered.set()
        if self.bot_hold is not None:
            await self.bot_hold.wait()
        if self.controller is not None:
            session = self.controller.store.find(session_id)
            if session and session.active:
                state = self.controller.native_state(session_id)
                socket = Socket(self.controller, session_id, REMOTE)
                self.controller.router(session_id).channels[REMOTE].attach(
                    socket, BOT_STREAM, generation, {})
                state.bot_ready.set()
                self.bot_socket = socket
        return BOT_SID

    async def mute_participant(self, conference_sid, call_sid, muted):
        self.operations.append(("mute", call_sid, muted))
        if call_sid == OWNER_SID and muted and self.fail_owner_mute:
            raise RuntimeError("Owner mute failed")
        if call_sid == OWNER_SID and muted and self.owner_mute_hold is not None:
            self.owner_mute_entered.set()
            await self.owner_mute_hold.wait()
        if call_sid == BOT_SID and muted and self.fail_bot_mute_404:
            raise TwilioRestException(404, "https://api.twilio.example", msg="Participant ended")
        self.actual_muted[call_sid] = muted

    async def end_call(self, call_sid):
        self.ended.append(call_sid)

    async def end_conference(self, conference_sid):
        self.ended_conferences.append(conference_sid)


class NativeHarness:
    def __init__(self, tmp_path):
        self.provider, self.registry, self.dialer = Provider(), Registry(), NativeDialer()
        self.store = OperatorSessions(NATIVE_SETTINGS)
        self.observed, self.delivered, self.ends = [], [], []
        self.voice = VoiceSettings(enabled=True, gemini_api_key="test", elevenlabs_api_key="test",
            output_dir=str(tmp_path), elevenlabs_voice_id="",
            elevenlabs_model="eleven_flash_v2_5")
        self.controller = OperatorController(NATIVE_SETTINGS, self.store, self.dialer,
            voice=self.voice, registry=self.registry, provider_transport=self.provider.transport(),
            on_native_audio=lambda *args: self.observed.append(args),
            on_agent_turn=lambda *args, **kwargs: self.delivered.append((args, kwargs)),
            on_call_end=lambda s: self.ends.append(s.id))
        self.dialer.controller = self.controller

    async def joined(self, *, observers=True):
        self.session, _ = await self.store.reserve_outbound(DESTINATION, "Get a quote", str(uuid.uuid4()))
        call = self.session
        call.native_conference = True
        await self.store.bind_call_sid(call.id, OWNER, OWNER_SID)
        await self.store.bind_call_sid(call.id, REMOTE, REMOTE_SID)
        call.canonical_call_sid = REMOTE_SID
        await self.store.mark_owner_prompt(call.id)
        await self.store.begin_remote_dial(call.id)
        self.state = self.controller.native_state(call.id)
        self.state.sid = CONFERENCE
        self.state.participants.update((OWNER, REMOTE))
        for role, stream in ((OWNER, OWNER_STREAM), (REMOTE, REMOTE_STREAM)):
            call.legs[role].attached = observers
            call.legs[role].stream_sid = stream if observers else ""
        await self.controller.native_connected(call.id)
        self.router = self.controller.router(call.id)
        return call

    async def close(self):
        await self.store.close()


class ReadSocket:
    """A finite inbound WebSocket; passive observers must never send media."""
    def __init__(self, events):
        self.events = list(events)
        self.sent, self.closed = [], []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive(self):
        if self.events:
            return {"type": "websocket.receive", "text": json.dumps(self.events.pop(0))}
        return {"type": "websocket.disconnect"}

    async def send_json(self, value):
        self.sent.append(value)

    async def close(self, code):
        self.closed.append(code)


def start_event(call, role):
    leg = call.legs[role]
    stream = OWNER_STREAM if role == OWNER else REMOTE_STREAM
    return {"event": "start", "streamSid": stream, "start": {
        "accountSid": NATIVE_SETTINGS.account_sid, "callSid": leg.call_sid,
        "streamSid": stream,
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        "customParameters": {"token": leg.token, "generation": str(leg.generation)}}}


def media_event(stream, track, marker, timestamp=0):
    return {"event": "media", "streamSid": stream, "media": {
        "track": track, "timestamp": str(timestamp),
        "payload": base64.b64encode(bytes([marker]) * 160).decode("ascii")}}
