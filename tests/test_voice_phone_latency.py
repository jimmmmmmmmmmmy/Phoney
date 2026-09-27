"""Phone-only first-text deadline, safe attempt traces, and stream cleanup."""
import asyncio
import json

import httpx
import pytest

from test_voice_agent import CONTENTS, KEY, frame, text_event
from voice_stack.agent import GeminiError, reply_events_with_retry


@pytest.mark.parametrize('stall', ['headers', 'body', 'thoughts'])
def test_first_text_stall_retries_promptly_and_closes_old_stream(stall):
    async def run():
        calls, closed, events = [], [], []
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                if stall == 'thoughts':
                    yield frame(text_event('private reasoning', thought=True))
                await asyncio.Future()
                yield b''
            async def aclose(self):
                closed.append(True)
        async def handle(request):
            calls.append(request)
            if len(calls) == 1:
                if stall == 'headers':
                    try:
                        await asyncio.Future()
                    finally:
                        closed.append(True)
                return httpx.Response(200, stream=Body(), headers={'x-private': KEY})
            return httpx.Response(200, content=frame(text_event('Ready.', finish='STOP')))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            async with asyncio.timeout(1):
                events = [e async for e in reply_events_with_retry(http, KEY, 'Secret prompt', CONTENTS,
                    first_text_timeout=.03, trace=True)]
        assert len(calls) == 2 and closed == [True]
        assert [e for e in events if e['kind'] == 'retry'] == [
            {'kind': 'retry', 'reason': 'first-text-timeout', 'attempt': 2}]
        traces = [e for e in events if e['kind'] == 'trace']
        assert [e['attempt'] for e in traces if e['stage'] == 'request-started'] == [1, 2]
        assert [e['attempt'] for e in traces if e['stage'] == 'first-text'] == [2]
        assert all(e['attempt_ms'] >= 0 and e['model'] for e in traces)
        assert KEY not in json.dumps(traces) and 'Secret prompt' not in json.dumps(traces)
        assert 'private reasoning' not in json.dumps(traces) and 'Ready.' not in json.dumps(traces)
        assert [e['text'] for e in events if e['kind'] == 'text'] == ['Ready.']
    asyncio.run(run())


def test_first_text_deadline_is_removed_once_speech_begins():
    async def run():
        calls = []
        async def body():
            yield frame(text_event('Already speaking.'))
            await asyncio.sleep(.08)
            yield frame(text_event(' Finished.', finish='STOP'))
        async def handle(request):
            calls.append(request)
            return httpx.Response(200, content=body())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            events = [e async for e in reply_events_with_retry(http, KEY, 'Rules', CONTENTS,
                first_text_timeout=.02, timeout=.5)]
        assert len(calls) == 1
        assert [e['kind'] for e in events] == ['text', 'text', 'complete']
    asyncio.run(run())


def test_repeated_first_text_stall_is_bounded_to_two_attempts():
    async def run():
        calls = []
        async def handle(request):
            calls.append(request)
            await asyncio.Future()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(GeminiError, match='spoken text in time'):
                async with asyncio.timeout(1):
                    async for _ in reply_events_with_retry(http, KEY, 'Rules', CONTENTS,
                            first_text_timeout=.02):
                        pass
        assert len(calls) == 2
    asyncio.run(run())


def test_generic_reply_does_not_get_the_phone_first_text_deadline():
    async def run():
        async def handle(request):
            await asyncio.sleep(.05)
            return httpx.Response(200, content=frame(text_event('Ready.', finish='STOP')))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            events = [e async for e in reply_events_with_retry(http, KEY, 'Rules', CONTENTS, timeout=.5)]
        assert [e['kind'] for e in events] == ['text', 'complete']
    asyncio.run(run())


def test_phone_first_text_retries_share_the_original_total_deadline():
    async def run():
        calls = []
        async def handle(request):
            calls.append(request)
            if len(calls) == 1:
                await asyncio.Future()
            await asyncio.sleep(.03)
            return httpx.Response(200, content=frame(text_event('Too late.', finish='STOP')))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(TimeoutError):
                async for _ in reply_events_with_retry(http, KEY, 'Rules', CONTENTS,
                        first_text_timeout=.04, timeout=.06):
                    pass
        assert len(calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize('cancel_attempt', [1, 2])
def test_phone_cancellation_closes_stream_without_replaying(cancel_attempt):
    async def run():
        calls, closed = [], []
        waiting = asyncio.Event()
        class Body(httpx.AsyncByteStream):
            def __init__(self, attempt):
                self.attempt = attempt
            async def __aiter__(self):
                if self.attempt == cancel_attempt:
                    waiting.set()
                await asyncio.Future()
                yield b''
            async def aclose(self):
                closed.append(self.attempt)
        async def handle(request):
            calls.append(request)
            return httpx.Response(200, stream=Body(len(calls)))
        async def consume(http):
            async for _ in reply_events_with_retry(http, KEY, 'Rules', CONTENTS,
                    first_text_timeout=.03, trace=True):
                pass
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            task = asyncio.create_task(consume(http))
            await asyncio.wait_for(waiting.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(calls) == cancel_attempt
        assert closed == list(range(1, cancel_attempt + 1))
    asyncio.run(run())


def test_runtime_uses_phone_deadline_and_emits_safe_attempt_timing(tmp_path, monkeypatch, caplog):
    from test_operator_keypad import Harness, Provider
    caplog.set_level('INFO', logger='uvicorn.error')
    monkeypatch.setattr('operator_service.runtime.FIRST_TEXT_SECONDS', .03)
    async def run():
        class StallFirst(Provider):
            def transport(self):
                transport = super().transport()
                self.gemini_calls = 0
                async def handle(request):
                    if 'generativelanguage' in request.url.host:
                        self.gemini_calls += 1
                        if self.gemini_calls == 1:
                            await asyncio.Future()
                    return await transport.handle_async_request(request)
                return httpx.MockTransport(handle)
        h = Harness(tmp_path, provider=StallFirst())
        s = await h.joined()
        await h.press('#1')
        await h.complete()
        assert s.active and h.provider.gemini_calls == 2
        traces = [json.loads(r.message.split('operator_trace ', 1)[1])
                  for r in caplog.records if r.message.startswith('operator_trace ')]
        attempts = [t for t in traces if t['event'].startswith('gemini-attempt-')]
        assert [t['attempt'] for t in attempts if t['event'].endswith('request-started')] == [1, 2]
        assert [t['attempt'] for t in attempts if t['event'].endswith('first-text')] == [2]
        assert any(t.get('reason') == 'first-text-timeout' for t in attempts)
        assert all('text' not in t and 'prompt' not in t and 'headers' not in t for t in attempts)
        await h.close()
    asyncio.run(run())
