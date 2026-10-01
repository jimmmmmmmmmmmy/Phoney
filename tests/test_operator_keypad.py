"""Focused product and boundary checks; test helpers live in support."""

from dataclasses import replace
import asyncio
import json

from operator_service import OperatorSessions
from operator_service.routes import OperatorController
from operator_service.sessions import AGENT, ANNOUNCING, CONNECTED, HUMAN, OWNER, REMOTE

from support.operator_keypad import Dialer, Harness, OWNER_STREAM, Provider, REMOTE_SID, REMOTE_STREAM, SETTINGS, Socket, until


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
