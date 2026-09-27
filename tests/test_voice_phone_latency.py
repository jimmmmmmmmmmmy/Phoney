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


@pytest.mark.parametrize(('cancel_attempt', 'retry_deadline'), [(1, True), (2, True), (1, False)])
def test_phone_cancellation_closes_stream_without_replaying(cancel_attempt, retry_deadline):
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
                    first_text_timeout=.03, retry_first_text_timeout=retry_deadline, trace=True):
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


@pytest.mark.parametrize('failure', ['empty', 'http', 'transport'])
def test_first_text_retry_opt_out_preserves_other_transient_retries(failure):
    async def run():
        calls = []
        async def handle(request):
            calls.append(request)
            if len(calls) == 1:
                if failure == 'empty':
                    return httpx.Response(200, content=frame({'candidates': [{'finishReason': 'STOP'}]}))
                if failure == 'http':
                    return httpx.Response(503)
                raise httpx.ReadError('Connection interrupted', request=request)
            return httpx.Response(200, content=frame(text_event('Ready.', finish='STOP')))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            events = [event async for event in reply_events_with_retry(http, KEY, 'Rules', CONTENTS,
                first_text_timeout=.03, retry_first_text_timeout=False)]
        assert len(calls) == 2
        assert [event['text'] for event in events if event['kind'] == 'text'] == ['Ready.']
        assert [event['reason'] for event in events if event['kind'] == 'retry'] == [{
            'empty': 'empty-response', 'http': 'http-503', 'transport': 'read-error',
        }[failure]]
    asyncio.run(run())


@pytest.mark.parametrize(('delay_stage', 'delay', 'scaled'), [
    ('headers', .045, True), ('body', .045, True), ('body', 3.2, False),
])
def test_voicemail_keeps_slow_first_request_until_readback_plays(
        tmp_path, monkeypatch, caplog, delay_stage, delay, scaled):
    from test_operator_keypad import until
    from test_operator_voicemail_integration import Harness, Provider, quick_timers
    from operator_service.sessions import REMOTE
    quick_timers(monkeypatch)
    caplog.set_level('INFO', logger='uvicorn.error')
    if scaled:
        monkeypatch.setattr('operator_service.runtime.FIRST_TEXT_SECONDS', .03)
        monkeypatch.setattr('operator_service.runtime.VOICEMAIL_FIRST_TEXT_SECONDS', .08)
    async def run():
        class DelayedProvider(Provider):
            def transport(self):
                good = super().transport()
                self.gemini_calls = 0
                async def handle(request):
                    if 'generativelanguage' not in request.url.host:
                        return await good.handle_async_request(request)
                    self.gemini_calls += 1
                    if delay_stage == 'headers':
                        await asyncio.sleep(delay)
                    response = await good.handle_async_request(request)
                    if delay_stage == 'body':
                        class Body(httpx.AsyncByteStream):
                            async def __aiter__(self):
                                await asyncio.sleep(delay)
                                yield response.content
                        return httpx.Response(200, stream=Body())
                    return response
                return httpx.MockTransport(handle)
        h = Harness(tmp_path, provider=DelayedProvider())
        try:
            s = await h.incoming()
            await h.controller.on_timeout(s, 'owner-no-answer')
            await h.ready()
            await h.controller.transcript(s.id, REMOTE, 'Alex called about the meeting at ten.', segment_id='m1')
            await until(lambda: len(h.delivered) >= 2)
            await h.ready()
            assert s.active and not s.voicemail_fallback
            assert h.provider.gemini_calls == 1 and not h.dialer.replacements
            assert h.provider.phases == ['readback']
            assert h.delivered[-1][0][1] == "Alex called about tomorrow's meeting at ten. Is that right?"
            assert h.delivered[-1][1]['delivery'] == 'played'
            traces = [json.loads(record.message.split('operator_trace ', 1)[1])
                      for record in caplog.records if record.message.startswith('operator_trace ')]
            starts = [trace for trace in traces if trace['event'] == 'gemini-attempt-request-started']
            assert len(starts) == 1
            assert starts[0]['first_text_timeout_seconds'] == (.08 if scaled else 4.0)
            assert starts[0]['retry_first_text_timeout'] is False
        finally:
            await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('stall', ['headers', 'body', 'thoughts'])
def test_voicemail_first_text_stall_keeps_same_voice_and_acknowledges(tmp_path, monkeypatch, stall):
    from test_operator_keypad import until
    from test_operator_voicemail_integration import Harness, Provider, quick_timers
    from operator_service.sessions import REMOTE
    quick_timers(monkeypatch)
    monkeypatch.setattr('operator_service.runtime.VOICEMAIL_FIRST_TEXT_SECONDS', .04)
    async def run():
        class StalledProvider(Provider):
            def transport(self):
                good = super().transport()
                self.gemini_calls, self.closed = 0, []
                closed = self.closed
                class Body(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        if stall == 'thoughts':
                            yield frame(text_event('private reasoning', thought=True))
                        await asyncio.Future()
                        yield b''
                    async def aclose(self):
                        closed.append(True)
                async def handle(request):
                    if 'generativelanguage' not in request.url.host:
                        return await good.handle_async_request(request)
                    self.gemini_calls += 1
                    if stall == 'headers':
                        try:
                            await asyncio.Future()
                        finally:
                            closed.append(True)
                    return httpx.Response(200, stream=Body())
                return httpx.MockTransport(handle)
        h = Harness(tmp_path, provider=StalledProvider())
        try:
            s = await h.incoming()
            await h.controller.on_timeout(s, 'owner-no-answer')
            await h.ready()
            await h.controller.transcript(s.id, REMOTE, 'Please call Alex back.', segment_id='m1')
            await until(lambda: len(h.delivered) >= 2)
            await h.ready()
            assert h.provider.gemini_calls == 1 and h.provider.closed == [True]
            assert s.active and not s.voicemail_fallback and not h.dialer.replacements
            assert 'anything else' in h.delivered[-1][0][1]
            assert 'Please call Alex back.' not in h.delivered[-1][0][1]
            assert s.voicemail_phase == 'followup'
            assert h.delivered[-1][1]['delivery'] == 'played'
            assert h.voicemails.get(s.canonical_call_sid)['mode'] == 'voicemail_ai'
            # Even a second model outage can acknowledge that nothing else is needed
            # on this same voice/stream, then hang up after the farewell plays.
            await h.controller.transcript(s.id, REMOTE, "No, that's all.", segment_id='m2')
            await until(lambda: not s.active)
            assert h.provider.gemini_calls == 2 and h.provider.closed == [True, True]
            assert not h.dialer.replacements
            assert h.delivered[-1][1]['delivery'] == 'played'
        finally:
            await h.close()
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
