"""Single-leg voicemail through real routing with fake providers and phone sockets."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from operator_service.routes import OperatorController
from operator_service.sessions import AGENT, OWNER, REMOTE, OperatorSessions
from operator_service.voicemail_agent import VoicemailAgent
from voicemail import VoicemailStore
from voice_stack.audio import FRAME_BYTES
from voice_stack.settings import VoiceSettings
from voice_stack.prompts import VOICEMAIL_GREETING
from test_operator_keypad import SETTINGS, Dialer, Socket, OWNER_SID, REMOTE_SID, REMOTE_STREAM, until


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
                phase = next(p for p in ("greeting", "readback", "confirm", "no_message", "unconfirmed")
                             if f"voicemail phase: {p}." in system)
                self.phases.append(phase)
                text = {
                    "greeting": "I'm the voicemail assistant. Please leave a message.",
                    "readback": "Alex called about tomorrow's meeting at ten. Is that right?",
                    "confirm": "Thank you, goodbye.\n[/END CALL]",
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


def test_unanswered_call_greets_reads_message_confirms_then_hangs_up(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    async def run():
        h = Harness(tmp_path)
        s = await h.incoming()
        await h.controller.on_timeout(s, "owner-no-answer")
        await h.ready()
        assert h.provider.phases == []
        assert h.delivered[0][0][1] == VOICEMAIL_GREETING
        assert h.delivered[0][1]['delivery'] == 'played'
        assert OWNER_SID in h.dialer.ended and REMOTE_SID not in h.dialer.ended
        assert not h.router.attached(OWNER) and s.voicemail
        await h.controller.transcript(s.id, REMOTE, "This is Alex. Tomorrow's meeting is at ten.", segment_id="m1")
        await until(lambda: h.provider.phases == ["readback"])
        await h.ready()
        req = [body for url, body in h.provider.requests if "generativelanguage" in url][-1]
        assert "Tomorrow's meeting is at ten" in json.dumps(req['contents'])
        assert VOICEMAIL_GREETING in json.dumps(req['contents'])
        assert s.active and h.delivered[-1][1]['delivery'] == 'played'
        await h.controller.transcript(s.id, REMOTE, "Yes, that's correct.", segment_id="m2")
        await until(lambda: not s.active)
        await h.store.wait_idle()
        assert h.provider.phases == ["readback", "confirm"]
        assert REMOTE_SID in h.dialer.ended
        assert h.delivered[-1][1]['delivery'] == 'played'
        assert all("[/END CALL]" not in body.get('text', '') for _, body in h.provider.requests)
        await h.close()
    asyncio.run(run())


def test_late_owner_socket_cannot_release_or_end_voicemail(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    async def run():
        h = Harness(tmp_path)
        s = await h.incoming()
        await h.controller.on_timeout(s, "owner-no-answer")
        await h.ready()
        await h.controller.stream_stopped(s.id, OWNER, "rejected-session-ended")
        assert s.active and s.mode == AGENT and s.voicemail
        assert h.provider.phases == []
        await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["gemini", "elevenlabs"])
def test_provider_failure_switches_once_to_native_recording(tmp_path, monkeypatch, provider):
    quick_timers(monkeypatch)
    class FailedProvider(Provider):
        def transport(self):
            good = super().transport()
            async def handle(request):
                target = "generativelanguage" if provider == "gemini" else "elevenlabs"
                if target in request.url.host:
                    return httpx.Response(503, text="unavailable")
                return await good.handle_async_request(request)
            return httpx.MockTransport(handle)
    async def run():
        h = Harness(tmp_path, FailedProvider())
        s = await h.incoming()
        await h.controller.on_timeout(s, "owner-no-answer")
        if provider == "gemini":
            await h.ready()
            assert not h.dialer.replacements
            await h.controller.transcript(s.id, REMOTE, "Please call Alex back.", segment_id="m1")
        await until(lambda: h.dialer.replacements)
        assert s.active and s.voicemail_fallback
        assert REMOTE_SID not in h.dialer.ended
        sid, xml = h.dialer.replacements[0]
        assert sid == REMOTE_SID and '<Record ' in xml and '<Say ' in xml
        assert 'transcribe="false"' in xml and '<Connect>' not in xml
        assert h.voicemails.get(REMOTE_SID)['mode'] == 'voicemail_fallback'
        assert h.voicemails.get(REMOTE_SID)['recording_status'] == 'awaiting'
        await h.controller.fallback_voicemail(s.id, "repeat-error")
        await h.controller.transcript(s.id, REMOTE, "Late STT must not revive the agent")
        await h.controller.stream_stopped(s.id, REMOTE, "socket-disconnected")
        assert len(h.dialer.replacements) == 1 and s.active
        assert s.id not in h.controller._voicemail_agents
        await h.close()
    asyncio.run(run())


def test_no_ready_voice_uses_native_recording_without_any_ai_provider(tmp_path, monkeypatch):
    async def run():
        h = Harness(tmp_path)
        h.controller.voice = None
        s = await h.incoming()
        await h.controller.on_timeout(s, "owner-no-answer")
        assert s.active and s.voicemail_fallback
        assert len(h.dialer.replacements) == 1 and h.provider.requests == []
        await h.close()
    asyncio.run(run())


def test_twilio_recording_failure_is_visible_and_ends_only_after_fallback_attempt(tmp_path):
    class FailedDialer(RecordingDialer):
        async def replace_twiml(self, *args):
            await super().replace_twiml(*args)
            raise RuntimeError("twilio-offline")
    async def run():
        h = Harness(tmp_path, dialer=FailedDialer())
        h.controller.voice = None
        s = await h.incoming()
        await h.controller.on_timeout(s, "owner-no-answer")
        await h.store.wait_idle()
        assert len(h.dialer.replacements) == 1 and not s.active
        assert h.voicemails.get(REMOTE_SID)['recording_status'] == 'failed'
        assert s.ended_reason == 'voicemail-recording-unavailable'
        await h.close()
    asyncio.run(run())


def test_no_input_farewell_and_hangup_are_driven_by_runtime_silence(tmp_path, monkeypatch):
    quick_timers(monkeypatch, initial_silence_seconds=.04)
    async def run():
        h = Harness(tmp_path)
        s = await h.incoming()
        await h.controller.on_timeout(s, "owner-no-answer")
        await until(lambda: not s.active)
        assert h.provider.phases == ["no_message"]
        assert h.delivered[-1][1]['delivery'] == 'played'
        await h.close()
    asyncio.run(run())


def test_remote_reconnect_resumes_listening_without_duplicate_greeting(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    async def run():
        h = Harness(tmp_path)
        s = await h.incoming()
        await h.controller.on_timeout(s, "owner-no-answer")
        await h.ready()
        h.router.channels[REMOTE].detach()
        s.legs[REMOTE].attached = False
        await h.controller.stream_stopped(s.id, REMOTE, "socket-disconnected")
        h.remote = Socket(h.controller, s.id, REMOTE)
        h.router.channels[REMOTE].attach(h.remote, REMOTE_STREAM, 2, s.legs[REMOTE].counters)
        s.legs[REMOTE].attached = True
        await h.controller.stream_started(s.id, REMOTE, REMOTE_STREAM)
        assert s.mode == AGENT and s.active
        await h.controller.transcript(s.id, REMOTE, "Please ask Alex to call me.", segment_id="after-recovery")
        await until(lambda: h.provider.phases == ["readback"])
        await h.ready()
        await h.close()
    asyncio.run(run())


def test_late_owner_rest_result_is_canceled_without_hanging_up_caller(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    class SlowDialer(Dialer):
        def __init__(self):
            super().__init__()
            self.gate = asyncio.Event()
        async def create_leg(self, **kwargs):
            self.created.append(kwargs)
            await self.gate.wait()
            return OWNER_SID
    async def run():
        dialer = SlowDialer()
        h = Harness(tmp_path, dialer=dialer)
        s = await h.incoming(bind_owner=False)
        task = h.store.spawn(h.controller._dial(s.id, OWNER, h.settings.owner_number, "<Response/>"))
        await until(lambda: dialer.created)
        await h.controller.on_timeout(s, "owner-no-answer")
        await h.ready()
        dialer.gate.set()
        await task
        await until(lambda: OWNER_SID in dialer.ended)
        assert s.active and REMOTE_SID not in dialer.ended
        assert h.provider.phases == []
        await h.close()
    asyncio.run(run())


def test_transport_loss_during_initial_voice_lookup_does_not_end_voicemail(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    async def run():
        h = Harness(tmp_path)
        s = await h.incoming()
        original = h.controller._internal_snapshot
        gate = asyncio.Event()
        async def blocked(kind):
            await gate.wait()
            return await original(kind)
        h.controller._internal_snapshot = blocked
        await h.controller.on_timeout(s, "owner-no-answer")
        await asyncio.sleep(0)
        h.router.channels[REMOTE].detach()
        s.legs[REMOTE].attached = False
        await h.controller.stream_stopped(s.id, REMOTE, "socket-disconnected")
        gate.set()
        await asyncio.sleep(.03)
        assert s.active and h.provider.requests == []
        h.remote = Socket(h.controller, s.id, REMOTE)
        h.router.channels[REMOTE].attach(h.remote, REMOTE_STREAM, 2, s.legs[REMOTE].counters)
        s.legs[REMOTE].attached = True
        await h.controller.stream_started(s.id, REMOTE, REMOTE_STREAM)
        await h.ready()
        assert h.provider.phases == []
        await h.close()
    asyncio.run(run())


def test_native_recording_stream_stop_recovers_missed_terminal_callbacks(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    monkeypatch.setattr('operator_service.routes.DISCONNECT_STATUS_DELAYS', (.001, .01))
    class StatusDialer(RecordingDialer):
        def __init__(self):
            super().__init__()
            self.statuses = ['in-progress', 'completed']
            self.reads = []
        async def read_status(self, call_sid):
            self.reads.append(call_sid)
            return {'status': self.statuses.pop(0), 'duration_seconds': 10}
    async def run():
        dialer = StatusDialer()
        h = Harness(tmp_path, provider=Provider(status=503), dialer=dialer)
        session = await h.incoming()
        await h.controller.on_timeout(session, 'owner-no-answer')
        await until(lambda: dialer.replacements)
        assert session.voicemail_fallback and session.active
        h.router.channels[REMOTE].detach()
        session.legs[REMOTE].attached = False
        await h.controller.stream_stopped(session.id, REMOTE, 'socket-disconnected')
        await until(lambda: not session.active)
        assert dialer.reads == [REMOTE_SID, REMOTE_SID]
        assert h.store.active_count == 0
        assert len(dialer.replacements) == 1
        await h.close()
    asyncio.run(run())
