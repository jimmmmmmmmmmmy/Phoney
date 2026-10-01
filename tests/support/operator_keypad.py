"""Shared fixtures and fakes for focused integration checks."""

import asyncio


import base64


from dataclasses import replace


import json


from types import SimpleNamespace


import uuid


import httpx


from config import Settings


from operator_service import OperatorSessions


from operator_service.controls import Keypad


from operator_service.routes import OperatorController


from operator_service.sessions import AGENT, ANNOUNCING, CONNECTED, HUMAN, OWNER, REMOTE


from voice_stack.audio import FRAME_BYTES


from voice_stack.settings import VoiceSettings


OWNER_SID, REMOTE_SID = "CA" + "1" * 32, "CA" + "2" * 32


OWNER_STREAM, REMOTE_STREAM = "MZ" + "3" * 32, "MZ" + "4" * 32


SETTINGS = Settings(account_sid="AC" + "a" * 32, auth_token="test-token",
    public_base_url="https://operator.example", twilio_number="+12025550102",
    owner_number="+12025550101", allowed_destinations=("+12025550103",),
    operator_admin_token="operator-admin-token-32-characters-long", voice_agent_enabled=True)


class Registry:
    def __init__(self):
        self.slots = {str(i): SimpleNamespace(id=f"agent-{i}", name=f"Agent {i}",
            prompt=f"Selected trusted instructions {i}", revision=1,
            voice_profile_id=f"profile-{i}", voice_id=f"voice{i}", slot=str(i)) for i in range(1,10)}
    def resolve_slot(self, slot):
        return self.slots.get(slot)


class Provider:
    def __init__(self, *, frames=3, status=200, delay=0, reply="Ready to help."):
        self.frames, self.status, self.delay, self.reply = frames, status, delay, reply
        self.requests = []
    def transport(self):
        async def handle(request):
            body = json.loads(request.content)
            self.requests.append((str(request.url), body))
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.status != 200:
                return httpx.Response(self.status, text="Provider unavailable")
            if "generativelanguage" in request.url.host:
                obj = {"candidates": [{"content": {"parts": [{"text": self.reply}]}, "finishReason": "STOP"}]}
                return httpx.Response(200, text="data: " + json.dumps(obj) + "\n\n",
                                      headers={"content-type": "text/event-stream"})
            cue = body["text"] == "An AI assistant is joining this call."
            return httpx.Response(200, content=bytes([0x10 if cue else 0x2A]) * FRAME_BYTES * (3 if cue else self.frames))
        return httpx.MockTransport(handle)


class Socket:
    def __init__(self, controller, session_id, role, *, acknowledge=True, block=False):
        self.controller, self.session_id, self.role = controller, session_id, role
        self.sent, self.acknowledge, self.block = [], acknowledge, block
    async def send_json(self, message):
        if self.block:
            await asyncio.Event().wait()
        self.sent.append(message)
        if message["event"] == "mark" and self.acknowledge:
            await self.controller.mark(self.session_id, self.role, message["mark"]["name"], "played")
    def frames(self, marker):
        return [base64.b64decode(m["media"]["payload"]) for m in self.sent
                if m["event"] == "media" and base64.b64decode(m["media"]["payload"])[0] == marker]
    def marks(self):
        return [m["mark"]["name"] for m in self.sent if m["event"] == "mark"]


class Dialer:
    def __init__(self):
        self.created, self.ended = [], []
    async def create_leg(self, **kwargs):
        self.created.append(kwargs)
        return OWNER_SID if len(self.created) == 1 else REMOTE_SID
    async def end_call(self, sid):
        self.ended.append(sid)


class Harness:
    def __init__(self, tmp_path, *, provider=None, voice=True, acknowledge=True):
        self.provider = provider or Provider()
        self.registry, self.dialer = Registry(), Dialer()
        self.store = OperatorSessions(SETTINGS)
        self.voice = VoiceSettings(enabled=True, gemini_api_key="test", elevenlabs_api_key="test",
            output_dir=str(tmp_path), elevenlabs_voice_id="") if voice else None
        self.delivered, self.output, self.starts, self.ends = [], [], [], []
        self.controller = OperatorController(SETTINGS, self.store, self.dialer, voice=self.voice,
            registry=self.registry, provider_transport=self.provider.transport(),
            on_call_start=lambda s: self.starts.append(s.canonical_call_sid),
            on_call_end=lambda s: self.ends.append(s.id),
            on_agent_turn=lambda *a, **kw: self.delivered.append((a,kw)),
            on_output_audio=lambda *a: self.output.append(a),
            keypad_factory=lambda: Keypad(prefix_seconds=.05, coalesce_seconds=.05))
        self.acknowledge = acknowledge
    async def joined(self):
        self.session, _ = await self.store.reserve_outbound("+12025550103", "Get an itemised quote", str(uuid.uuid4()))
        s = self.session
        await self.store.bind_call_sid(s.id, OWNER, OWNER_SID)
        await self.store.bind_call_sid(s.id, REMOTE, REMOTE_SID)
        await self.store.mark_owner_prompt(s.id)
        await self.store.begin_remote_dial(s.id)
        self.router = self.controller.router(s.id)
        self.owner = Socket(self.controller, s.id, OWNER)
        self.remote = Socket(self.controller, s.id, REMOTE, acknowledge=self.acknowledge)
        for role, socket, sid in ((OWNER,self.owner,OWNER_STREAM),(REMOTE,self.remote,REMOTE_STREAM)):
            self.router.channels[role].attach(socket, sid, 1, s.legs[role].counters)
            self.router.generations[role] = 1
            s.legs[role].stream_sid = sid
            s.legs[role].attached = True
        await self.controller.stream_started(s.id, REMOTE, REMOTE_STREAM)
        return s
    async def press(self, keys):
        for key in keys:
            await self.controller.dtmf(self.session.id, key)
            await asyncio.sleep(0)
    async def close(self):
        await self.store.close()
    async def complete(self):
        await until(lambda: self.session.mode == AGENT and not self.controller.playing(self.session.id))


async def until(check, timeout=5):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(.005)
