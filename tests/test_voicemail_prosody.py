"""Natural voicemail phrasing and recovery phase contracts through fake phone audio."""
import asyncio
import json

import httpx
import pytest

from operator_service.sessions import REMOTE
from test_operator_keypad import until
from test_operator_voicemail_integration import Harness, Provider, quick_timers


def response(text):
    payload = {'candidates': [{'content': {'parts': [{'text': text}]}, 'finishReason': 'STOP'}]}
    return httpx.Response(200, text='data: ' + json.dumps(payload) + '\n\n')


def test_concise_recap_reaches_synthesis_without_a_mid_clause_split(tmp_path, monkeypatch):
    quick_timers(monkeypatch)
    recap = ("Taylor, you found a one-bedroom studio near the college for $1,200 a month, "
             "and you're calling to say that it's in good condition. Is that right?")
    assert 120 < len(recap) < 240

    class NaturalReply(Provider):
        def transport(self):
            good = super().transport()
            async def handle(request):
                if 'generativelanguage' in request.url.host:
                    return response(recap)
                return await good.handle_async_request(request)
            return httpx.MockTransport(handle)

    async def run():
        h = Harness(tmp_path, NaturalReply())
        try:
            s = await h.incoming()
            await h.controller.on_timeout(s, 'owner-no-answer')
            await h.ready()
            await h.controller.transcript(s.id, REMOTE,
                "Taylor here. A one-bedroom studio near college, $1,200 a month, in good condition.",
                segment_id='message', speech_final=True)
            await until(lambda: len(h.delivered) == 2)
            await h.ready()
            assert h.delivered[-1][0][1] == recap
            assert h.provider.requests[-1][1]['text'] == recap
            assert len(h.provider.requests) == 2  # Greeting and one coherent recap.
            assert s.active and not h.dialer.replacements
        finally:
            await h.close()
    asyncio.run(run())


@pytest.mark.parametrize(('answer', 'spoken', 'end'), [
    ('Yes.', 'Go ahead.', False),
    ("No, that's all.", 'Thank you for your message. Goodbye.\n[/END CALL]', True),
])
def test_recovered_gemini_gets_followup_question_context_not_readback_confirmation(
        tmp_path, monkeypatch, answer, spoken, end):
    quick_timers(monkeypatch)
    monkeypatch.setattr('operator_service.runtime.VOICEMAIL_FIRST_TEXT_SECONDS', .03)

    class RecoveringProvider(Provider):
        def transport(self):
            good = super().transport()
            self.model_requests = []
            async def handle(request):
                if 'generativelanguage' not in request.url.host:
                    return await good.handle_async_request(request)
                body = json.loads(request.content)
                self.model_requests.append(body)
                if len(self.model_requests) == 1:
                    await asyncio.Future()
                return response(spoken)
            return httpx.MockTransport(handle)

    async def run():
        h = Harness(tmp_path, RecoveringProvider())
        try:
            s = await h.incoming()
            await h.controller.on_timeout(s, 'owner-no-answer')
            await h.ready()
            await h.controller.transcript(s.id, REMOTE, 'Please call Taylor about the apartment.',
                segment_id='message', speech_final=True)
            await until(lambda: len(h.delivered) == 2)
            await h.ready()
            assert 'anything else' in h.delivered[-1][0][1]
            await h.controller.transcript(s.id, REMOTE, answer, segment_id='answer', speech_final=True)
            if end:
                await until(lambda: not s.active)
            else:
                await until(lambda: len(h.delivered) == 3)
                await h.ready()
                assert s.active and h.delivered[-1][0][1] == 'Go ahead.'
            req = h.provider.model_requests[-1]
            assert 'voicemail phase: followup.' in json.dumps(req['systemInstruction'])
            assert 'NOT a question asking whether a readback was correct' in json.dumps(req['systemInstruction'])
            assert 'anything else' in json.dumps(req['contents'])
            assert not h.dialer.replacements
        finally:
            await h.close()
    asyncio.run(run())
