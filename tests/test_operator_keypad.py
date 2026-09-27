"""Manual dialogue over real routing with mocked Gemini, TTS, and phone sockets."""
import asyncio
import base64
from dataclasses import replace
import json
from types import SimpleNamespace
import uuid
import xml.etree.ElementTree as ET

import httpx
import pytest

from config import Settings
from operator_service import OperatorSessions
from operator_service.controls import Keypad
from operator_service.routes import OperatorController
from operator_service.sessions import AGENT, ANNOUNCING, CONNECTED, HUMAN, OWNER, PREPARING, REMOTE, OperatorRejected
from voice_stack.audio import FRAME_BYTES
from voice_stack.settings import VoiceSettings

OWNER_SID, REMOTE_SID = "CA" + "1" * 32, "CA" + "2" * 32
OWNER_STREAM, REMOTE_STREAM = "MZ" + "3" * 32, "MZ" + "4" * 32
OWNER_FRAME = bytes([0x11]) * FRAME_BYTES
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


def test_manual_nine_uses_selected_prompt_voice_and_current_transcript(tmp_path):
    async def run():
        h=Harness(tmp_path); s=await h.joined()
        await h.controller.transcript(s.id, OWNER, "Budget is six thousand.", segment_id="one")
        await h.controller.transcript(s.id, REMOTE, "Can you confirm the budget?", segment_id="two")
        assert h.provider.requests == []
        await h.press("#9"); await h.complete()
        gemini=[body for url,body in h.provider.requests if "generativelanguage" in url][0]
        assert "Selected trusted instructions 9" in json.dumps(gemini["systemInstruction"])
        assert "Budget is six thousand" in json.dumps(gemini["contents"])
        assert all("/voice9/stream" in url for url,_ in h.provider.requests if "elevenlabs" in url)
        assert s.agent_name == "Agent 9" and s.profile == "9"
        assert h.delivered[0][1]["delivery"] == "played"
        assert h.starts == [REMOTE_SID]
        await h.close()
    asyncio.run(run())


def test_announcement_is_remote_only_and_acknowledged_before_agent(tmp_path):
    async def run():
        h=Harness(tmp_path, acknowledge=False); s=await h.joined()
        await h.press("#1")
        await until(lambda: bool(h.remote.marks()))
        assert s.mode == ANNOUNCING
        assert len(h.remote.frames(0x10)) == 3 and not h.owner.frames(0x10)
        assert not h.remote.frames(0x2A)
        h.remote.acknowledge = True
        await h.controller.mark(s.id, REMOTE, h.remote.marks()[0], "played")
        await h.complete()
        assert len(h.remote.frames(0x2A)) == 3 and len(h.owner.frames(0x2A)) == 3
        await h.close()
    asyncio.run(run())


def test_real_length_reply_preserves_all_audio_and_provenance(tmp_path):
    async def run():
        h=Harness(tmp_path,provider=Provider(frames=125)); s=await h.joined()
        await h.press("#1"); await h.complete()
        assert len(h.remote.frames(0x2A)) == 125
        assert h.router.pending_agent(REMOTE) == 0
        assert s.legs[REMOTE].counters["gaps"] == 0
        assert len([a for a in h.output if a[-1]=="agent"]) == 125
        assert len([a for a in h.output if a[-1]=="announcement"]) == 3
        await h.close()
    asyncio.run(run())


def test_zero_during_preparation_restores_human_without_any_audio(tmp_path):
    async def run():
        h=Harness(tmp_path,provider=Provider(delay=.4)); s=await h.joined()
        await h.press("#1")
        await until(lambda:s.mode==PREPARING)
        assert s.mode == PREPARING
        assert h.router.forward(OWNER, OWNER_FRAME)
        await h.press("#0")
        await asyncio.sleep(.5)
        assert s.mode == HUMAN and not h.remote.frames(0x10) and not h.remote.frames(0x2A)
        assert h.delivered == []
        await h.close()
    asyncio.run(run())


def test_zero_during_reply_clears_audio_and_records_interruption(tmp_path):
    async def run():
        h=Harness(tmp_path,provider=Provider(frames=100)); s=await h.joined()
        await h.press("#1")
        await until(lambda: len(h.remote.frames(0x2A))>=4)
        await h.press("#0")
        count=len(h.remote.frames(0x2A)); await asyncio.sleep(.1)
        assert len(h.remote.frames(0x2A)) == count and s.mode == HUMAN
        assert h.delivered[-1][1]["delivery"] == "interrupted"
        assert not any(t["speaker"]=="agent" for t in s.turns)
        assert h.router.forward(OWNER, OWNER_FRAME)
        await h.close()
    asyncio.run(run())


def test_remote_barge_in_cancels_reply_then_final_turn_gets_new_reply(tmp_path):
    async def run():
        h=Harness(tmp_path,provider=Provider(frames=40)); s=await h.joined()
        await h.press("#1"); await until(lambda: len(h.remote.frames(0x2A))>=2)
        await h.controller.transcript(s.id, REMOTE, "Actually", final=False)
        assert s.mode == AGENT
        count=len(h.remote.frames(0x2A)); await asyncio.sleep(.1)
        assert len(h.remote.frames(0x2A)) == count
        await h.controller.transcript(s.id, REMOTE, "Actually the budget is five thousand.", segment_id="change")
        await until(lambda: len([u for u,b in h.provider.requests if "generativelanguage" in u])==2)
        await h.complete()
        requests=[body for url,body in h.provider.requests if "generativelanguage" in url]
        assert "five thousand" in json.dumps(requests[-1]["contents"])
        assert len([u for u,b in h.provider.requests if b.get("text")=="An AI assistant is joining this call."])==1
        await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("status",[403,429,503])
def test_provider_failure_returns_to_human(tmp_path,status):
    async def run():
        h=Harness(tmp_path,provider=Provider(status=status)); s=await h.joined()
        await h.press("#1"); await until(lambda:s.mode==HUMAN)
        assert not h.remote.frames(0x2A) and not h.delivered
        await h.close()
    asyncio.run(run())


def test_missing_slot_or_disabled_voice_does_not_mute_or_call_provider(tmp_path):
    async def run():
        h=Harness(tmp_path,voice=False); s=await h.joined()
        await h.press("#1")
        assert s.mode==HUMAN and h.provider.requests==[]
        h.controller.voice=VoiceSettings(enabled=True,gemini_api_key="x",elevenlabs_api_key="x",output_dir=str(tmp_path))
        h.registry.slots.pop("1")
        await h.press("#1")
        assert s.mode==HUMAN and h.provider.requests==[]
        await h.close()
    asyncio.run(run())


def test_repeated_shortcut_is_idempotent_but_different_slot_cancels_old_voice(tmp_path):
    async def run():
        h=Harness(tmp_path,provider=Provider(frames=30)); s=await h.joined()
        await h.press("#1"); await until(lambda:len(h.remote.frames(0x2A))>=2)
        epoch=s.reply_epoch
        await h.press("#1"); assert s.reply_epoch==epoch
        await h.press("#2"); await until(lambda:s.reply_epoch==epoch+1)
        assert s.agent_name=="Agent 2"
        await h.complete()
        assert any("/voice2/stream" in u for u,b in h.provider.requests)
        await h.close()
    asyncio.run(run())


def test_remote_keys_and_stale_owner_generation_cannot_take_over(tmp_path):
    async def run():
        h=Harness(tmp_path); s=await h.joined()
        for role in (REMOTE,OWNER):
            if role==OWNER: h.router.generations[OWNER]=2
            for i,key in enumerate("#1"):
                await h.router._queue_dtmf(role,s.legs[role],{"event":"dtmf","sequenceNumber":str(i),
                    "streamSid":s.legs[role].stream_sid,"dtmf":{"digit":key}})
        assert s.mode==HUMAN and h.provider.requests==[]
        await h.close()
    asyncio.run(run())


def test_incomplete_prefix_expires_and_bare_digit_remains_ivr_input(tmp_path):
    async def run():
        h=Harness(tmp_path); s=await h.joined(); digits=[]
        async def record(sid,value): digits.append(value)
        h.controller.digit_sender=record
        await h.press("#"); await asyncio.sleep(.1); await h.press("1")
        await until(lambda:digits==["1"])
        assert s.mode==HUMAN and h.provider.requests==[]
        await h.close()
    asyncio.run(run())


def test_hangup_cancels_provider_work_and_notifies_end(tmp_path):
    async def run():
        h=Harness(tmp_path,provider=Provider(delay=.4)); s=await h.joined()
        await h.press("#1")
        await h.controller.end(s.id,"test-end")
        await asyncio.sleep(.05)
        assert not h.controller.playing(s.id) and h.ends==[s.id]
        assert not h.remote.frames(0x2A)
        await h.close()
    asyncio.run(run())


def test_snapshot_is_stable_during_current_activation(tmp_path):
    async def run():
        h=Harness(tmp_path); s=await h.joined()
        await h.press("#1"); await h.complete()
        h.registry.slots["1"]=SimpleNamespace(id="other",name="Changed",prompt="Changed prompt",voice_id="different",slot="1")
        await h.controller.transcript(s.id,REMOTE,"Please continue",segment_id="next")
        await until(lambda:len([u for u,b in h.provider.requests if "generativelanguage" in u])==2)
        await h.complete()
        assert s.agent_name=="Agent 1" and not any("/different/" in u for u,b in h.provider.requests)
        await h.close()
    asyncio.run(run())


def test_inbound_bridge_binds_existing_caller_and_dials_only_owner(tmp_path):
    async def run():
        settings=replace(SETTINGS,operator_inbound_enabled=True,agent_management_enabled=True,
                         media_capture_enabled=True,transcription_enabled=True,
                         deepgram_api_key="test", media_storage_dir=str(tmp_path / "capture"), transcript_storage_dir=str(tmp_path / "transcripts"), workspace_storage_dir=str(tmp_path / "workspace"),
                         allowed_destinations=())
        store=OperatorSessions(settings); dialer=Dialer(); starts=[]
        controller=OperatorController(settings,store,dialer,on_call_start=lambda s:starts.append(s.canonical_call_sid))
        session=await controller.start_inbound(REMOTE_SID,"+12025550199")
        await until(lambda:len(dialer.created)==1)
        assert dialer.created[0]["to"]==SETTINGS.owner_number
        assert "<Connect>" in controller.inbound_twiml(session)
        router=controller.router(session.id)
        for role,sid in ((OWNER,OWNER_STREAM),(REMOTE,REMOTE_STREAM)):
            router.channels[role].attach(Socket(controller,session.id,role),sid,1,session.legs[role].counters)
            session.legs[role].stream_sid=sid
        await controller.stream_started(session.id,REMOTE,REMOTE_STREAM)
        await controller.stream_started(session.id,OWNER,OWNER_STREAM)
        await controller.dtmf(session.id,"1")
        assert session.phase==CONNECTED and session.mode==HUMAN
        assert len(dialer.created)==1 and starts==[REMOTE_SID]
        assert await controller.start_inbound(REMOTE_SID,"+12025550199") is session
        await store.close()
    asyncio.run(run())


def test_missing_announcement_ack_returns_control_without_agent_speech(tmp_path,monkeypatch):
    monkeypatch.setattr("operator_service.runtime.PLAYBACK_ACK_SECONDS",-0.9)
    async def run():
        h=Harness(tmp_path,acknowledge=False); s=await h.joined()
        await h.press("#1"); await until(lambda:s.mode==HUMAN)
        assert not h.remote.frames(0x2A) and not h.delivered
        await h.close()
    asyncio.run(run())


def test_stalled_output_writer_returns_to_human(tmp_path,monkeypatch):
    monkeypatch.setattr("operator_service.runtime.FRAME_STALL_SECONDS",.08)
    async def run():
        h=Harness(tmp_path,provider=Provider(frames=40)); s=await h.joined()
        await h.press("#1"); await until(lambda:len(h.remote.frames(0x2A))>=2)
        h.remote.block=True
        await until(lambda:s.mode==HUMAN and bool(h.delivered))
        assert h.delivered[-1][1]["delivery"]=="interrupted"
        await h.close()
    asyncio.run(run())


def test_release_during_registry_lookup_cancels_pending_takeover(tmp_path):
    async def run():
        h=Harness(tmp_path); s=await h.joined(); started=asyncio.Event(); finish=asyncio.Event()
        async def delayed(slot):
            started.set(); await finish.wait(); return h.registry.slots[slot]
        h.registry.resolve_slot=delayed
        # The media reader returns even while the registry lookup is blocked.
        async with asyncio.timeout(.2):
            await h.press("#1")
        await started.wait()
        assert h.router.forward(OWNER,OWNER_FRAME)
        await h.press("#0"); finish.set()
        await h.store.wait_idle()
        assert s.mode==HUMAN and h.provider.requests==[]
        await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("async_getter", [False, True])
def test_unhealthy_transcription_refuses_takeover_before_selecting_a_profile(tmp_path, async_getter):
    async def run():
        h=Harness(tmp_path); s=await h.joined()
        def unavailable(session):
            raise RuntimeError("Transcription stopped")
        async def unavailable_async(session):
            await asyncio.sleep(0)
            unavailable(session)
        h.controller.context_getter=unavailable_async if async_getter else unavailable
        with pytest.raises(OperatorRejected) as rejected:
            await h.controller.takeover(s.id,"1")
        assert rejected.value.reason=="transcription-unavailable"
        assert s.mode==HUMAN and s.agent_snapshot is None
        assert h.router.forward(OWNER,OWNER_FRAME)
        assert h.provider.requests==[] and not h.remote.marks()
        await h.close()
    asyncio.run(run())


def test_release_during_context_preflight_cancels_pending_takeover(tmp_path):
    async def run():
        h=Harness(tmp_path); s=await h.joined(); started=asyncio.Event(); finish=asyncio.Event()
        # A synchronous adapter may itself return an awaitable.
        async def delayed_context():
            started.set(); await finish.wait(); return []
        h.controller.context_getter=lambda session:delayed_context()
        takeover=asyncio.create_task(h.controller.takeover(s.id,"1"))
        await started.wait()
        assert h.router.forward(OWNER,OWNER_FRAME)
        await h.press("#0"); finish.set()
        with pytest.raises(OperatorRejected) as rejected:
            await takeover
        assert rejected.value.reason=="takeover-canceled"
        assert s.mode==HUMAN and s.agent_snapshot is None and h.provider.requests==[]
        await h.close()
    asyncio.run(run())


def test_later_context_failure_returns_agent_control_to_the_owner(tmp_path):
    async def run():
        h=Harness(tmp_path); s=await h.joined()
        h.controller.context_getter=lambda session:session.turns
        await h.controller.takeover(s.id,"1"); await h.complete()
        requests=len(h.provider.requests)
        def unavailable(session):
            raise RuntimeError("Transcription stopped")
        h.controller.context_getter=unavailable
        await h.controller.transcript(s.id,REMOTE,"Are you still there?",segment_id="later")
        await until(lambda:s.mode==HUMAN)
        assert h.router.forward(OWNER,OWNER_FRAME)
        assert len(h.provider.requests)==requests
        await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("capture,transcription,detection", [
    (False,False,False),
    (True,False,False),
    (True,True,False),
    (True,False,True),
    (True,True,True),
])
def test_inbound_greeting_is_exact_before_streaming(
        tmp_path,capture,transcription,detection):
    async def run():
        h=Harness(tmp_path); s=await h.joined()
        h.controller.settings=replace(SETTINGS,media_capture_enabled=capture,
            transcription_enabled=transcription,modulate_detection_enabled=detection,
            media_storage_dir=str(tmp_path/"capture"),transcript_storage_dir=str(tmp_path/"transcripts"),
            detection_storage_dir=str(tmp_path/"detection"),deepgram_api_key="test",modulate_api_key="test")
        response=ET.fromstring(h.controller.inbound_twiml(s))
        assert response[0].tag=="Say" and response[0].text=="New College Data Science"
        assert len(response.findall("Say")) == 1
        assert response[1].tag=="Connect"
        assert response.find("Connect/Stream") is not None
        await h.close()
    asyncio.run(run())


def test_duplicate_final_transcript_does_not_trigger_duplicate_reply(tmp_path):
    async def run():
        h=Harness(tmp_path); s=await h.joined()
        await h.press("#1"); await h.complete()
        for _ in range(3):
            await h.controller.transcript(s.id,REMOTE,"Next question",segment_id="same-final")
        await until(lambda:len([u for u,b in h.provider.requests if "generativelanguage" in u])==2)
        await h.complete(); await asyncio.sleep(.4)
        assert len([u for u,b in h.provider.requests if "generativelanguage" in u])==2
        await h.close()
    asyncio.run(run())


def test_new_final_turn_during_preparation_rebuilds_context(tmp_path):
    async def run():
        h=Harness(tmp_path,provider=Provider(delay=.1)); s=await h.joined()
        await h.press("#1")
        await until(lambda:len([u for u,b in h.provider.requests if "generativelanguage" in u])==1)
        assert s.mode==PREPARING
        await h.controller.transcript(s.id,OWNER,"Actually ask about a red truck instead.",segment_id="latest")
        await h.complete()
        requests=[body for url,body in h.provider.requests if "generativelanguage" in url]
        assert len(requests)==2 and "red truck" in json.dumps(requests[-1]["contents"])
        assert len(h.remote.frames(0x10))==3
        await h.close()
    asyncio.run(run())


def test_entire_synthesis_stream_has_a_deadline(tmp_path,monkeypatch):
    async def slow_stream(*args,**kwargs):
        for _ in range(50):
            await asyncio.sleep(.15)
            yield bytes([0x2A])*FRAME_BYTES
    monkeypatch.setattr("operator_service.runtime.speech_bytes",slow_stream)
    async def run():
        h=Harness(tmp_path); s=await h.joined()
        h.controller.voice=replace(h.voice,request_timeout=1)
        await h.press("#1")
        await until(lambda:s.mode==HUMAN and bool(h.delivered))
        assert len(h.remote.frames(0x2A))<50
        assert h.delivered[-1][1]["delivery"]=="interrupted"
        await h.close()
    asyncio.run(run())


def test_output_timestamps_are_sample_monotonic_even_with_a_frozen_clock(tmp_path):
    async def run():
        h=Harness(tmp_path); s=await h.joined()
        h.controller.elapsed_ms=lambda sid:17
        for _ in range(3): h.controller.output_audio(s.id,OWNER_FRAME,"human")
        assert [row[2] for row in h.output]==[17,37,57]
        await h.close()
    asyncio.run(run())


def test_continuous_transcript_does_not_starve_manual_handoff(tmp_path):
    async def run():
        h = Harness(tmp_path, provider=Provider(delay=.15))
        s = await h.joined()
        await h.press('#1')
        await until(lambda: s.mode == PREPARING)
        epoch = s.reply_epoch
        # Every update used to cancel synthesis and start the same cue again.
        for i in range(30):
            if s.mode != PREPARING:
                break
            await h.controller.transcript(s.id, OWNER, f'Updated detail {i}.', segment_id=f'turn-{i}')
            assert s.reply_epoch == epoch
            assert h.router.forward(OWNER, OWNER_FRAME)
            await asyncio.sleep(.03)
        assert s.mode in (ANNOUNCING, AGENT)
        await h.complete()
        cues = [b for u, b in h.provider.requests if 'elevenlabs' in u and b['text'] == 'An AI assistant is joining this call.']
        assert len(cues) == 1
        requests = [b for u, b in h.provider.requests if 'generativelanguage' in u]
        assert len(requests) == 2
        assert 'Updated detail' in json.dumps(requests[-1]['contents'])
        await h.close()
    asyncio.run(run())


def test_repeated_active_shortcut_does_not_revoke_end_call_or_restart(tmp_path):
    async def run():
        h = Harness(tmp_path, provider=Provider(delay=.1))
        s = await h.joined()
        await h.press('#1')
        await until(lambda: s.mode == PREPARING)
        request = h.controller._takeover_requests[s.id]
        epoch = s.reply_epoch
        await h.press('#1#1')
        assert h.controller._takeover_requests[s.id] == request
        assert s.reply_epoch == epoch
        await h.complete()
        assert len([u for u, b in h.provider.requests if 'generativelanguage' in u]) == 1
        await h.close()
    asyncio.run(run())


def test_first_reply_synthesis_runs_while_announcement_is_pending(tmp_path, monkeypatch):
    async def run():
        cue_started, reply_started, release_cue = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def cue(*args, **kwargs):
            cue_started.set()
            await release_cue.wait()
            return bytes([0x10]) * FRAME_BYTES * 3
        async def reply(*args, **kwargs):
            reply_started.set()
            yield bytes([0x2A]) * FRAME_BYTES * 3
        monkeypatch.setattr('operator_service.runtime.speech', cue)
        monkeypatch.setattr('operator_service.runtime.speech_bytes', reply)
        h = Harness(tmp_path)
        s = await h.joined()
        await h.press('#1')
        async with asyncio.timeout(1):
            await cue_started.wait()
            await reply_started.wait()
        assert s.mode == PREPARING
        assert h.router.forward(OWNER, OWNER_FRAME)
        release_cue.set()
        await h.complete()
        await h.close()
    asyncio.run(run())


def test_cached_announcement_and_private_timing_trace(tmp_path, caplog):
    caplog.set_level('INFO', logger='uvicorn.error')
    async def run():
        h = Harness(tmp_path)
        s = await h.joined()
        await h.press('#1'); await h.complete()
        await h.press('#0#1'); await h.complete()
        cues = [b for u, b in h.provider.requests if 'elevenlabs' in u and b['text'] == 'An AI assistant is joining this call.']
        assert len(cues) == 1
        traces = [json.loads(r.message.split('operator_trace ', 1)[1]) for r in caplog.records if r.message.startswith('operator_trace ')]
        events = {r['event'] for r in traces}
        assert {'shortcut','gemini-first-text','elevenlabs-first-audio','announcement-cache-hit','playback-acknowledged','reply-played'} <= events
        assert all(r['call_sid'] == REMOTE_SID and r['elapsed_ms'] >= 0 for r in traces)
        assert all('text' not in r and 'prompt' not in r and 'voice_id' not in r for r in traces)
        await h.close()
    asyncio.run(run())


def test_disconnect_recovers_terminal_provider_status_and_duration(tmp_path, monkeypatch):
    monkeypatch.setattr('operator_service.routes.DISCONNECT_STATUS_DELAYS', (.01,))
    async def run():
        h = Harness(tmp_path); s = await h.joined()
        async def read_status(sid):
            assert sid == REMOTE_SID
            return {'status': 'completed', 'duration_seconds': 35}
        h.dialer.read_status = read_status
        s.legs[REMOTE].attached = False
        h.router.channels[REMOTE].detach()
        await h.controller.stream_stopped(s.id, REMOTE, 'socket-disconnected')
        await until(lambda: not s.active)
        await h.store.wait_idle()
        assert s.duration_seconds == 35 and s.ended_reason == 'remote-completed'
        assert not h.controller._disconnect_checks
        await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('reconnect', [False, True])
def test_disconnect_live_status_or_new_stream_never_ends_call(tmp_path, monkeypatch, reconnect):
    monkeypatch.setattr('operator_service.routes.DISCONNECT_STATUS_DELAYS', (.01,))
    async def run():
        h = Harness(tmp_path); s = await h.joined()
        started, finish = asyncio.Event(), asyncio.Event()
        async def read_status(sid):
            started.set()
            await finish.wait()
            return {'status': 'completed' if reconnect else 'in-progress', 'duration_seconds': 35}
        h.dialer.read_status = read_status
        s.legs[REMOTE].attached = False
        await h.controller.stream_stopped(s.id, REMOTE, 'socket-disconnected')
        await started.wait()
        if reconnect:
            s.legs[REMOTE].attached = True
            s.legs[REMOTE].generation += 1
            await h.controller.stream_started(s.id, REMOTE, REMOTE_STREAM)
        finish.set()
        await h.store.wait_idle()
        assert s.active and h.dialer.ended == []
        assert not h.controller._disconnect_checks
        await h.close()
    asyncio.run(run())
