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


@pytest.mark.parametrize('failure,reason', [
    (httpx.ReadTimeout, 'read-timeout'),
    (httpx.ConnectTimeout, 'connect-timeout'),
    (httpx.WriteTimeout, 'write-timeout'),
    (httpx.PoolTimeout, 'pool-timeout'),
    (httpx.ConnectError, 'connect-error'),
    (httpx.ReadError, 'read-error'),
    (httpx.WriteError, 'write-error'),
    (httpx.CloseError, 'close-error'),
    (408, 'http-408'),
    (429, 'http-429'),
    (500, 'http-500'),
    (502, 'http-502'),
    (503, 'http-503'),
    (504, 'http-504'),
])
def test_transient_failure_before_speech_retries_once_and_can_complete(failure, reason):
    async def run():
        calls = []

        async def handle(request):
            calls.append(request)
            if len(calls) == 1:
                if isinstance(failure, int):
                    return httpx.Response(failure, text='provider details must not enter trace')
                raise failure('provider details must not enter trace', request=request)
            return httpx.Response(200, content=frame(text_event('Please leave your message.', finish='STOP')))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            events = [e async for e in reply_events_with_retry(http, KEY, 'Phone rules', CONTENTS)]
        assert len(calls) == 2
        assert events[0] == {'kind': 'retry', 'reason': reason, 'attempt': 2}
        assert [e['kind'] for e in events] == ['retry', 'text', 'complete']
        assert events[1]['text'] == 'Please leave your message.'

    asyncio.run(run())


@pytest.mark.parametrize('failure', [httpx.ReadTimeout, 503])
def test_repeated_transient_failure_is_bounded_to_two_requests(failure):
    async def run():
        calls = []
        events = []

        async def handle(request):
            calls.append(request)
            if isinstance(failure, int):
                return httpx.Response(failure)
            raise failure('unavailable', request=request)

        expected = httpx.HTTPStatusError if isinstance(failure, int) else failure
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(expected):
                async for event in reply_events_with_retry(http, KEY, 'Phone rules', CONTENTS):
                    events.append(event)
        assert len(calls) == 2
        assert len(events) == 1 and events[0]['kind'] == 'retry'

    asyncio.run(run())


@pytest.mark.parametrize('failure', [httpx.ReadTimeout, httpx.ReadError])
def test_stream_transport_failure_after_text_never_replays_speech(failure):
    async def run():
        calls = []
        events = []

        async def body(request):
            yield frame(text_event('Already speaking.'))
            raise failure('stream stopped', request=request)

        async def handle(request):
            calls.append(request)
            return httpx.Response(200, content=body(request))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(failure):
                async for event in reply_events_with_retry(http, KEY, 'Phone rules', CONTENTS):
                    events.append(event)
        assert len(calls) == 1
        assert events == [{'kind': 'text', 'text': 'Already speaking.'}]

    asyncio.run(run())


@pytest.mark.parametrize('status', [400, 401, 403, 404, 409, 422, 501])
def test_permanent_http_failure_does_not_retry(status):
    async def run():
        calls = []

        async def handle(request):
            calls.append(request)
            return httpx.Response(status)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(httpx.HTTPStatusError):
                async for _ in reply_events_with_retry(http, KEY, 'Phone rules', CONTENTS):
                    pytest.fail('Permanent failures must not emit a retry or speech event')
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize('body', [
    b'data: {not json}\n\n',
    frame({'promptFeedback': {'blockReason': 'SAFETY'}}),
    frame({'candidates': [{'finishReason': 'SAFETY'}]}),
    frame({'candidates': [{'finishReason': 'MAX_TOKENS'}]}),
    frame({'error': {'code': 403}}),
    frame({'candidates': [{'content': {'parts': [{'functionCall': {'name': 'unexpected'}}]}}]}),
])
def test_blocked_malformed_and_explicit_truncated_responses_do_not_retry(body):
    async def run():
        calls = []

        async def handle(request):
            calls.append(request)
            return httpx.Response(200, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(GeminiError):
                async for _ in reply_events_with_retry(http, KEY, 'Phone rules', CONTENTS):
                    pytest.fail('Rejected output must not emit a retry or speech event')
        assert len(calls) == 1

    asyncio.run(run())


def test_retry_keeps_the_original_total_generation_deadline():
    async def run():
        calls = []
        events = []

        async def handle(request):
            calls.append(request)
            if len(calls) == 1:
                await asyncio.sleep(0.04)
                raise httpx.ReadTimeout('first attempt stalled', request=request)
            # Would succeed within a fresh 0.1s budget, but the first attempt
            # has already spent part of the one deadline for this reply.
            await asyncio.sleep(0.08)
            return httpx.Response(200, content=frame(text_event('Too late.', finish='STOP')))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(TimeoutError):
                async for event in reply_events_with_retry(http, KEY, 'Phone rules', CONTENTS, timeout=0.1):
                    events.append(event)
        assert len(calls) == 2
        assert events == [{'kind': 'retry', 'reason': 'read-timeout', 'attempt': 2}]

    asyncio.run(run())


@pytest.mark.parametrize('cancel_attempt', [1, 2])
def test_external_cancellation_propagates_without_an_extra_request(cancel_attempt):
    async def run():
        calls = []
        events = []
        waiting = asyncio.Event()
        request_cancelled = asyncio.Event()

        async def handle(request):
            calls.append(request)
            if len(calls) < cancel_attempt:
                raise httpx.ConnectError('connect failed', request=request)
            waiting.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                request_cancelled.set()
                raise

        async def consume(http):
            async for event in reply_events_with_retry(http, KEY, 'Phone rules', CONTENTS):
                events.append(event)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            task = asyncio.create_task(consume(http))
            await asyncio.wait_for(waiting.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert request_cancelled.is_set()
        assert len(calls) == cancel_attempt
        assert len(events) == cancel_attempt - 1

    asyncio.run(run())
