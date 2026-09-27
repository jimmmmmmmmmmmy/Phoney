import asyncio

import httpx
import pytest

from test_voice_agent import CONTENTS, KEY, frame, text_event
from voice_stack.agent import GeminiError, reply_events_with_retry


def test_empty_stop_retries_once_before_emitting_speech():
    async def run():
        calls = []
        async def handle(request):
            calls.append(request)
            event = ({'candidates': [{'finishReason': 'STOP', 'content': {'parts': []}}]}
                     if len(calls) == 1 else text_event('What else should I know?', finish='STOP'))
            return httpx.Response(200, content=frame(event))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            events = [e async for e in reply_events_with_retry(http, KEY, 'Phone rules', CONTENTS)]
        assert len(calls) == 2
        assert [e['kind'] for e in events] == ['retry', 'text', 'complete']
    asyncio.run(run())


@pytest.mark.parametrize('partial', [False, True])
def test_repeat_empty_is_bounded_and_partial_speech_is_never_replayed(partial):
    async def run():
        calls = []
        async def handle(request):
            calls.append(request)
            event = (text_event('Already speaking.', finish='MAX_TOKENS') if partial else
                     {'candidates': [{'finishReason': 'STOP', 'content': {'parts': []}}]})
            return httpx.Response(200, content=frame(event))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(GeminiError):
                async for _ in reply_events_with_retry(http, KEY, 'Phone rules', CONTENTS):
                    pass
        assert len(calls) == (1 if partial else 2)
    asyncio.run(run())
