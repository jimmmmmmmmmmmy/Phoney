"""Single-leg voicemail through real routing with fake providers and phone sockets."""
import asyncio
import json
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi import FastAPI
from twilio.request_validator import RequestValidator

from operator_service.routes import OperatorController, register_operator_routes
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


def test_readback_tts_failure_preserves_message_and_closes_once(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    class FailedProvider(Provider):
        fail = False
        def transport(self):
            good = super().transport()
            async def handle(request):
                if self.fail and 'elevenlabs' in request.url.host:
                    return httpx.Response(503, text="unavailable")
                return await good.handle_async_request(request)
            return httpx.MockTransport(handle)
    async def run():
        h = Harness(tmp_path, FailedProvider())
        s = await h.incoming()
        await h.controller.on_timeout(s, "owner-no-answer")
        await h.ready()
        assert not h.dialer.replacements
        h.provider.fail = True
        await h.controller.transcript(s.id, REMOTE, "Please call Alex back.", segment_id="m1")
        await until(lambda: h.dialer.replacements)
        assert s.active and s.voicemail_fallback and s.voicemail_phase == 'complete'
        assert REMOTE_SID not in h.dialer.ended
        sid, xml = h.dialer.replacements[0]
        root = ET.fromstring(xml)
        assert sid == REMOTE_SID and [node.tag for node in root] == ['Say', 'Hangup']
        assert root.find('Say').text == (
            'Thank you for your message. I cannot read it back right now. Goodbye.')
        assert h.voicemails.get(REMOTE_SID)['mode'] == 'voicemail_ai'
        assert h.voicemails.get(REMOTE_SID)['recording_status'] == 'awaiting'
        assert any(t['text'] == 'Please call Alex back.' for t in s.turns)
        turns = list(s.turns)
        requests = len(h.provider.requests)
        await h.controller.fallback_voicemail(s.id, "repeat-error")
        await h.controller.transcript(s.id, REMOTE, "Late STT must not revive the agent")
        await h.controller.stream_stopped(s.id, REMOTE, "socket-disconnected")
        assert len(h.dialer.replacements) == 1 and s.active
        assert len(h.provider.requests) == requests and s.turns == turns
        assert s.id not in h.controller._voicemail_agents
        # Storage remains local and can finish from the normal terminal callback.
        h.voicemails.finish_ai(REMOTE_SID, available=True, duration=35)
        assert h.voicemails.get(REMOTE_SID)['recording_status'] == 'completed'
        await h.close()
    asyncio.run(run())


def test_gemini_failure_keeps_voicemail_on_same_stream_and_voice(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    class FailedGemini(Provider):
        def transport(self):
            good = super().transport()
            async def handle(request):
                if 'generativelanguage' in request.url.host:
                    self.requests.append((str(request.url), json.loads(request.content)))
                    return httpx.Response(503, text='unavailable')
                return await good.handle_async_request(request)
            return httpx.MockTransport(handle)
    async def run():
        h = Harness(tmp_path, FailedGemini())
        s = await h.incoming()
        await h.controller.on_timeout(s, 'owner-no-answer')
        await h.ready()
        stream = s.legs[REMOTE].stream_sid
        vm = h.controller._voicemail_agents[s.id]
        await h.controller.transcript(s.id, REMOTE, 'Please call Alex back.',
                                      segment_id='message', speech_final=True)
        await until(lambda: vm.has_message and not h.controller.playing(s.id))
        readback = ' '.join(args[1] for args, _ in h.delivered[1:])
        assert 'Please call Alex back.' not in readback and 'anything else' in readback
        assert vm.followup_mode and s.voicemail_phase == 'followup'
        assert s.active and not s.voicemail_fallback and not h.dialer.replacements
        assert h.controller._voicemail_agents[s.id] is vm
        assert s.legs[REMOTE].stream_sid == stream and s.legs[REMOTE].attached
        assert h.voicemails.get(REMOTE_SID)['mode'] == 'voicemail_ai'
        assert all(metadata['delivery'] == 'played' for _, metadata in h.delivered)
        assert REMOTE_SID not in h.dialer.ended
        await h.controller.transcript(s.id, REMOTE, "No, that's all.",
                                      segment_id='confirmation', speech_final=True)
        await until(lambda: not s.active)
        await h.store.wait_idle()
        assert REMOTE_SID in h.dialer.ended and not h.dialer.replacements
        assert h.delivered[-1][0][1].endswith('Goodbye.')
        assert h.delivered[-1][1]['delivery'] == 'played'
        speech_urls = [url for url, _ in h.provider.requests if 'elevenlabs' in url]
        assert speech_urls and all('/text-to-speech/owner-voice/stream' in url for url in speech_urls)
        model_requests = [body for url, body in h.provider.requests if 'generativelanguage' in url]
        assert len(model_requests) == 4
        assert 'anything else' in json.dumps(model_requests[-1]['contents'])
        assert 'voicemail phase: followup.' in json.dumps(model_requests[-1]['systemInstruction'])
        assert h.voicemails.get(REMOTE_SID)['mode'] == 'voicemail_ai'
        await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('evidence', ['none', 'vad', 'interim', 'stale-final', 'owner-final',
                                      'open-message', 'open-correction'])
def test_fallback_without_finished_caller_message_still_records(tmp_path, monkeypatch, evidence):
    quick_timers(monkeypatch)
    async def run():
        h = Harness(tmp_path)
        s = await h.incoming()
        await h.controller.on_timeout(s, 'owner-no-answer')
        await h.ready()
        vm = h.controller._voicemail_agents[s.id]
        floor = h.controller._caller_turn_floor[s.id]
        if evidence == 'vad':
            await h.controller.transcript(s.id, REMOTE, '', final=False, speech_started=True,
                                          speech_final=False, timestamp_ms=floor + 1)
        elif evidence == 'interim':
            await h.controller.transcript(s.id, REMOTE, 'Please call', final=False,
                                          speech_final=False, timestamp_ms=floor + 1)
        elif evidence == 'stale-final':
            await h.controller.transcript(s.id, REMOTE, 'Old speech.', segment_id='stale',
                                          speech_final=True, timestamp_ms=floor - 1)
        elif evidence == 'owner-final':
            await h.controller.transcript(s.id, OWNER, 'Not a caller message.', segment_id='owner')
        elif evidence in {'open-message', 'open-correction'}:
            await h.controller.transcript(s.id, REMOTE, 'Please call Alex.', segment_id='message',
                speech_final=evidence == 'open-correction', timestamp_ms=floor + 1)
            if evidence == 'open-correction':
                await h.controller.transcript(s.id, REMOTE, 'Actually', final=False,
                    speech_final=False, timestamp_ms=floor + 2)
        assert vm.has_final_message == (evidence in {'open-message', 'open-correction'})
        await h.controller.fallback_voicemail(s.id, 'transcription-unavailable')
        xml = ET.fromstring(h.dialer.replacements[0][1])
        assert [node.tag for node in xml] == ['Say', 'Record', 'Hangup']
        assert xml.find('Record').attrib['transcribe'] == 'false'
        assert h.voicemails.get(REMOTE_SID)['mode'] == 'voicemail_fallback'
        assert s.active and s.voicemail_phase == 'recording'
        await h.close()
    asyncio.run(run())


def test_saved_message_closing_transport_failure_retains_local_receipt(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    class FailedDialer(RecordingDialer):
        async def replace_twiml(self, *args):
            await super().replace_twiml(*args)
            raise RuntimeError('twilio-offline')
    async def run():
        h = Harness(tmp_path, dialer=FailedDialer())
        s = await h.incoming()
        await h.controller.on_timeout(s, 'owner-no-answer')
        await h.ready()
        await h.controller.transcript(s.id, REMOTE, 'Please call Alex back.', segment_id='message')
        assert not await h.controller.fallback_voicemail(s.id, 'dialogue-timeout')
        assert len(h.dialer.replacements) == 1 and not s.active
        assert s.ended_reason == 'voicemail-closing-unavailable'
        assert h.voicemails.get(REMOTE_SID)['mode'] == 'voicemail_ai'
        h.voicemails.finish_ai(REMOTE_SID, available=True, duration=35)
        assert h.voicemails.get(REMOTE_SID)['recording_status'] == 'completed'
        await h.close()
    asyncio.run(run())


def test_native_closing_reconnect_waits_for_terminal_status(tmp_path):
    async def run():
        h = Harness(tmp_path)
        app = FastAPI()
        controller = register_operator_routes(app, h.settings, h.store, dialer=h.dialer,
            voicemail_store=h.voicemails,
            on_call_end=lambda session: h.voicemails.finish_ai(
                session.canonical_call_sid, available=True, duration=35))
        s, _ = await h.store.reserve_inbound(REMOTE_SID, '+12025550199')
        assert await h.store.claim_voicemail(s.id)
        h.voicemails.start(REMOTE_SID, 'owner-no-answer', mode='voicemail_ai')
        vm = VoicemailAgent(s, on_reply=lambda _: None, on_end=lambda _: None)
        controller._voicemail_agents[s.id] = vm
        vm.transcript('Please call Alex back.', speech_final=True)
        await controller.fallback_voicemail(s.id, 'dialogue-timeout')
        assert s.active and not h.dialer.ended
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url=h.settings.public_base_url) as client:
            async def signed(path, **extra):
                body = {'AccountSid': h.settings.account_sid, 'CallSid': REMOTE_SID} | extra
                signature = RequestValidator(h.settings.auth_token).compute_signature(
                    h.settings.public_base_url + path, body)
                return await client.post(path, data=body, headers={'X-Twilio-Signature': signature})
            reconnect = await signed(f'/twilio/reconnect/{s.id}/remote')
            assert reconnect.status_code == 200
            assert reconnect.text == h.dialer.replacements[0][1]
            assert [node.tag for node in ET.fromstring(reconnect.text)] == ['Say', 'Hangup']
            assert s.active and not h.dialer.ended
            status = await signed(f'/twilio/status/{s.id}/remote',
                                  CallStatus='completed', CallDuration='35')
            assert status.status_code == 204 and not s.active
            record = h.voicemails.get(REMOTE_SID)
            assert record['mode'] == 'voicemail_ai' and record['recording_status'] == 'completed'
            assert record['recording_sid'] == ''
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


@pytest.mark.parametrize('phase', ['no_message', 'unconfirmed', 'complete'])
@pytest.mark.parametrize('end_marker', [True, False])
@pytest.mark.parametrize('activity_final', [True, False])
def test_new_caller_speech_during_terminal_preparation_keeps_voicemail_open(
        tmp_path, monkeypatch, phase, end_marker, activity_final):
    quick_timers(monkeypatch, pause_seconds=.1)
    async def run():
        started, resume = asyncio.Event(), asyncio.Event()
        generated = []
        async def reply(http, key, system, contents, **kwargs):
            generated.append((system, contents))
            if len(generated) == 1:
                assert f'voicemail phase: {phase}.' in system
                started.set()
                await resume.wait()
                text = 'Thank you. Goodbye.' + ('\n[/END CALL]' if end_marker else '')
            else:
                text = 'You asked Alex to call tomorrow. Is that right?'
            yield {'kind': 'text', 'text': text}
            yield {'kind': 'complete'}
        monkeypatch.setattr('operator_service.runtime.reply_events', reply)
        h = Harness(tmp_path)
        s = await h.incoming()
        await h.controller.on_timeout(s, 'owner-no-answer')
        await h.ready()
        vm = h.controller._voicemail_agents[s.id]
        vm.has_message = phase != 'no_message'
        await vm._request(phase)
        await asyncio.wait_for(started.wait(), 1)
        caller_onset = h.controller.elapsed_ms(s.id)
        await h.controller.transcript(s.id, REMOTE, 'Please ask Alex to call tomorrow.',
            final=activity_final, segment_id='late-message' if activity_final else '',
            speech_final=activity_final, timestamp_ms=caller_onset)
        await asyncio.sleep(.01)
        resume.set()
        await h.ready()
        assert s.active and REMOTE_SID not in h.dialer.ended
        if not activity_final:
            assert len(generated) == 1
            assert h.controller._caller_turn_floor[s.id] > caller_onset
            await h.controller.transcript(s.id, REMOTE, 'Please ask Alex to call tomorrow.',
                segment_id='late-message', speech_final=True,
                timestamp_ms=caller_onset)
        await until(lambda: len(generated) == 2)
        expected = 'readback' if phase == 'no_message' else 'confirm'
        assert f'voicemail phase: {expected}.' in generated[-1][0]
        assert 'Please ask Alex to call tomorrow.' in json.dumps(generated[-1][1])
        await h.ready()
        assert s.active and REMOTE_SID not in h.dialer.ended
        await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['greeting', 'readback'])
@pytest.mark.parametrize('evidence', ['vad-only', 'interim', 'final'])
def test_voicemail_playback_only_yields_to_recognized_caller_words(
        tmp_path, monkeypatch, caplog, phase, evidence):
    quick_timers(monkeypatch)
    caplog.set_level('INFO', logger='uvicorn.error')
    class LongSpeech(Provider):
        def transport(self):
            original = super().transport()
            async def handle(request):
                if 'elevenlabs' in request.url.host:
                    return httpx.Response(200, content=bytes([0x2A]) * FRAME_BYTES * 45)
                return await original.handle_async_request(request)
            return httpx.MockTransport(handle)
    async def run():
        h = Harness(tmp_path, provider=LongSpeech())
        s = await h.incoming()
        await h.controller.on_timeout(s, 'owner-no-answer')
        if phase == 'readback':
            await h.ready()
            await h.controller.transcript(s.id, REMOTE, 'Ask Alex to call tomorrow.',
                segment_id='message', speech_final=True)
        await until(lambda: s.id in h.controller._dialogue_runs
            and h.controller._dialogue_runs[s.id].voicemail_phase == phase
            and h.controller._dialogue_runs[s.id].speaking_started_ms is not None)
        epoch = s.reply_epoch
        clear_count = sum(m['event'] == 'clear' for m in h.remote.sent)
        revision = h.controller._remote_revisions.get(s.id, 0)
        if evidence == 'vad-only':
            await h.controller.transcript(s.id, REMOTE, '', final=False,
                speech_final=False, speech_started=True,
                timestamp_ms=h.controller.elapsed_ms(s.id))
            assert s.reply_epoch == epoch
            assert h.controller._remote_revisions.get(s.id, 0) == revision
            await h.ready()
            assert h.delivered[-1][1]['delivery'] == 'played'
            assert sum(m['event'] == 'clear' for m in h.remote.sent) == clear_count
        else:
            await h.controller.transcript(s.id, REMOTE, 'Wait, the meeting is at eleven.',
                final=evidence == 'final', segment_id='correction' if evidence == 'final' else '',
                speech_final=False, timestamp_ms=h.controller.elapsed_ms(s.id))
            await until(lambda: not h.controller.playing(s.id) and h.delivered
                        and h.delivered[-1][1]['delivery'] == 'interrupted')
            assert s.reply_epoch > epoch
            assert h.delivered[-1][1]['delivery'] == 'interrupted'
            count = len(h.remote.frames(0x2A))
            await asyncio.sleep(.08)
            assert len(h.remote.frames(0x2A)) == count
            assert sum(m['event'] == 'clear' for m in h.remote.sent) > clear_count
        traces = [json.loads(r.message.split('operator_trace ', 1)[1])
                  for r in caplog.records if r.message.startswith('operator_trace ')]
        interruptions = [t for t in traces if t['event'] == 'caller-interruption']
        if evidence == 'vad-only':
            assert not interruptions
        else:
            assert len(interruptions) == 1
            assert interruptions[0]['source'] == evidence + '-transcript'
            assert 'text' not in interruptions[0]
        assert s.active and REMOTE_SID not in h.dialer.ended
        await h.close()
    asyncio.run(run())
