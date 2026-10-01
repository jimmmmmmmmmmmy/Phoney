"""Shared fixtures and fakes for focused integration checks."""

import asyncio


import json


from types import SimpleNamespace


import httpx


from operator_service.routes import OperatorController


from operator_service.sessions import AGENT, OWNER, REMOTE, OperatorSessions


from operator_service.voicemail_agent import VoicemailAgent


from voicemail import VoicemailStore


from voice_stack.audio import FRAME_BYTES


from voice_stack.settings import VoiceSettings


from voice_stack.prompts import VOICEMAIL_GREETING


from support.operator_keypad import (
    SETTINGS,
    Dialer,
    Socket,
    OWNER_SID,
    REMOTE_SID,
    REMOTE_STREAM,
    until,
)


class Registry:
    def snapshot(self):
        return {"voices": [{"id": "owner-profile", "name": "owner", "voiceId": "owner-voice",
                            "ready": True, "available": True}]}


class Provider:
    def __init__(self, *, status=200):
        self.requests, self.phases = [], []
        self.status = status
        self.gate = None
    def transport(self):
        async def handle(request):
            body = json.loads(request.content)
            self.requests.append((str(request.url), body))
            if self.gate is not None:
                await self.gate.wait()
            if self.status != 200:
                return httpx.Response(self.status, text="unavailable")
            if "generativelanguage" in request.url.host:
                system = json.dumps(body["systemInstruction"])
                phase = next(p for p in ("greeting", "readback", "confirm", "no_message", "unconfirmed", "followup", "followup_timeout")
                             if f"voicemail phase: {p}." in system)
                self.phases.append(phase)
                text = {
                    "greeting": "I'm the voicemail assistant. Please leave a message.",
                    "readback": "Alex called about tomorrow's meeting at ten. Is that right?",
                    "confirm": "Thank you, goodbye.\n[/END CALL]",
                    "followup": "Thank you, goodbye.\n[/END CALL]",
                    "followup_timeout": "Thank you for your message. Goodbye.\n[/END CALL]",
                    "no_message": "I did not hear a message. Please call again. Goodbye.\n[/END CALL]",
                    "unconfirmed": "I heard your message but could not confirm the details. Goodbye.\n[/END CALL]",
                }[phase]
                data = {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}
                return httpx.Response(200, text="data: " + json.dumps(data) + "\n\n",
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(200, content=bytes([0x2A]) * FRAME_BYTES * 3)
        return httpx.MockTransport(handle)


class RecordingDialer(Dialer):
    def __init__(self):
        super().__init__()
        self.replacements = []
    async def replace_twiml(self, call_sid, twiml):
        self.replacements.append((call_sid, twiml))


class Harness:
    def __init__(self, tmp_path, provider=None, dialer=None):
        self.settings = SimpleNamespace(**(vars(SETTINGS) | {
            "operator_inbound_enabled": True, "voicemail_agent_enabled": True,
            "voicemail_agent_ring_seconds": 10, "automatic_takeover_enabled": False,
            "voicemail_enabled": False, "voicemail_storage_dir": str(tmp_path / "voicemails")}))
        self.provider = provider or Provider()
        self.store = OperatorSessions(self.settings)
        self.dialer = dialer or RecordingDialer()
        self.voicemails = VoicemailStore(self.settings)
        self.delivered = []
        voice = VoiceSettings(enabled=True, gemini_api_key="test", elevenlabs_api_key="test",
                              output_dir=str(tmp_path))
        self.controller = OperatorController(self.settings, self.store, self.dialer,
            voice=voice, registry=Registry(), provider_transport=self.provider.transport(),
            voicemail_store=self.voicemails,
            on_agent_turn=lambda *a, **kw: self.delivered.append((a, kw)))
    async def incoming(self, *, bind_owner=True):
        self.session, _ = await self.store.reserve_inbound(REMOTE_SID, "+12025550199")
        s = self.session
        await self.store.begin_owner_dial(s.id)
        if bind_owner:
            await self.store.bind_call_sid(s.id, OWNER, OWNER_SID)
        self.router = self.controller.router(s.id)
        self.remote = Socket(self.controller, s.id, REMOTE)
        self.router.channels[REMOTE].attach(self.remote, REMOTE_STREAM, 1, s.legs[REMOTE].counters)
        self.router.generations[REMOTE] = 1
        s.legs[REMOTE].stream_sid = REMOTE_STREAM
        s.legs[REMOTE].attached = True
        await self.controller.stream_started(s.id, REMOTE, REMOTE_STREAM)
        return s
    async def ready(self):
        await until(lambda: self.session.mode == AGENT and not self.controller.playing(self.session.id))
    async def close(self):
        await self.store.close()
        await self.voicemails.close()


def quick_timers(monkeypatch, **changes):
    def factory(*args, **kwargs):
        return VoicemailAgent(*args, **kwargs,
            **({"pause_seconds": .04, "initial_silence_seconds": 2,
                "confirmation_silence_seconds": 2, "capture_seconds": 3,
                "total_seconds": 8} | changes))
    monkeypatch.setattr("operator_service.routes.VoicemailAgent", factory)
