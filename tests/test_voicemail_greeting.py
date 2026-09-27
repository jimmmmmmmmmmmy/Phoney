"""Fixed greeting playback, ringing warm-up, and bounded synthesis caching."""
import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from operator_service.runtime import voicemail_greeting_audio
from operator_service.sessions import CONNECTED, HUMAN, OWNER, REMOTE
from test_operator_keypad import OWNER_STREAM, Socket, REMOTE_SID, until
from test_operator_voicemail_integration import Harness, Provider, quick_timers
from voice_stack.audio import FRAME_BYTES
from voice_stack.prompts import VOICEMAIL_GREETING


def test_ringing_warms_and_playback_reuses_the_same_inflight_synthesis(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    async def run():
        provider = Provider()
        provider.gate = asyncio.Event()
        h = Harness(tmp_path, provider)
        await h.controller.start_inbound(REMOTE_SID, '+12025550199')
        await until(lambda: provider.requests)
        assert len(provider.requests) == 1
        assert 'elevenlabs' in provider.requests[0][0]
        assert provider.requests[0][1]['text'] == VOICEMAIL_GREETING
        session = await h.incoming()
        assert session.mode == HUMAN and not session.voicemail and not h.delivered
        await h.controller.on_timeout(session, 'owner-no-answer')
        await until(lambda: sum(h.controller._voicemail_greeting_waiters.values()) == 2)
        assert len(provider.requests) == 1 and not h.delivered
        provider.gate.set()
        await h.ready()
        assert provider.phases == []
        assert len(provider.requests) == 1
        assert h.delivered[0][0][1] == VOICEMAIL_GREETING
        assert h.delivered[0][1]['delivery'] == 'played'
        assert not h.controller._voicemail_greeting_tasks
        assert not h.controller._voicemail_greeting_waiters
        assert not h.controller._voicemail_warmers
        await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('finish', ['hangup', 'answer'])
def test_warmup_is_canceled_when_caller_leaves_or_human_answers(tmp_path, finish):
    async def run():
        provider = Provider()
        provider.gate = asyncio.Event()
        h = Harness(tmp_path, provider)
        await h.controller.start_inbound(REMOTE_SID, '+12025550199')
        await until(lambda: provider.requests)
        session = await h.incoming()
        if finish == 'answer':
            owner = Socket(h.controller, session.id, OWNER)
            h.router.channels[OWNER].attach(owner, OWNER_STREAM, 1, session.legs[OWNER].counters)
            session.legs[OWNER].attached = True
            session.legs[OWNER].stream_sid = OWNER_STREAM
            await h.controller.stream_started(session.id, OWNER, OWNER_STREAM)
            assert session.phase == CONNECTED and session.mode == HUMAN and not session.voicemail
        else:
            await h.controller.end(session.id, 'caller-hangup')
        await until(lambda: not h.controller._voicemail_greeting_tasks)
        assert not h.controller._voicemail_warmers
        assert not h.controller._voicemail_greeting_waiters
        assert not h.controller.announcement_cache
        assert not h.delivered
        assert not h.dialer.replacements
        await h.close()
    asyncio.run(run())


def test_disabled_voicemail_does_not_warm_provider_audio(tmp_path):
    async def run():
        h = Harness(tmp_path)
        h.settings.voicemail_agent_enabled = False
        await h.controller.start_inbound(REMOTE_SID, '+12025550199')
        await h.store.wait_idle()
        assert h.provider.requests == []
        assert not h.controller._voicemail_warmers
        await h.close()
    asyncio.run(run())


def test_greeting_cache_is_voice_and_model_specific_and_bounded(tmp_path):
    async def run():
        h = Harness(tmp_path)
        snapshot = await h.controller._internal_snapshot('voicemail')
        first = await voicemail_greeting_audio(h.controller, snapshot)
        assert await voicemail_greeting_audio(h.controller, snapshot) == first
        assert len(h.provider.requests) == 1
        for index in range(16):
            await voicemail_greeting_audio(h.controller, replace(snapshot, voice_id=f'other{index}'))
        assert len(h.controller.announcement_cache) == 16
        assert len(h.provider.requests) == 17
        h.controller.voice = replace(h.controller.voice, elevenlabs_model='eleven_turbo_v2_5')
        await voicemail_greeting_audio(h.controller, replace(snapshot, voice_id='other15'))
        assert len(h.provider.requests) == 18
        assert len(h.controller.announcement_cache) == 16
        assert not h.controller._voicemail_greeting_tasks
        assert not h.controller._voicemail_greeting_waiters
        await h.close()
    asyncio.run(run())


def test_caller_can_interrupt_greeting_without_replaying_it(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    class LongGreeting(Provider):
        def transport(self):
            good = super().transport()
            async def handle(request):
                result = await good.handle_async_request(request)
                if 'elevenlabs' in request.url.host and json.loads(request.content)['text'] == VOICEMAIL_GREETING:
                    return httpx.Response(200, content=bytes([0x2A]) * FRAME_BYTES * 35)
                return result
            return httpx.MockTransport(handle)
    async def run():
        h = Harness(tmp_path, LongGreeting())
        session = await h.incoming()
        await h.controller.on_timeout(session, 'owner-no-answer')
        await until(lambda: h.controller._agent_frames_sent.get(session.id, 0) > 0)
        await h.controller.transcript(session.id, REMOTE, "This is Alex. Please call me back.",
            segment_id='during-greeting', timestamp_ms=h.controller.elapsed_ms(session.id))
        await until(lambda: h.provider.phases == ['readback'])
        await h.ready()
        greetings = [body for url, body in h.provider.requests
                     if 'elevenlabs' in url and body.get('text') == VOICEMAIL_GREETING]
        assert len(greetings) == 1
        assert h.delivered[0][0][1] == VOICEMAIL_GREETING
        assert h.delivered[0][1]['delivery'] == 'interrupted'
        assert session.active and not session.voicemail_fallback
        await h.close()
    asyncio.run(run())
