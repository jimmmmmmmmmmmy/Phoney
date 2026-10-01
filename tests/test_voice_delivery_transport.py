"""Exercise the provider wire protocol locally; never synthesize paid audio."""

import asyncio
import base64
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from websockets.asyncio.client import connect as real_connect
from websockets.asyncio.server import serve

from voice_stack import relay, tts
from voice_stack.delivery import DEFAULT_DELIVERY, LEGACY_MODEL, LATEST_REALTIME_MODEL


async def _verify_relay_continuity(monkeypatch):
    """Offline rendering shares completed phrase context without changing words."""
    requests, sessions, deltas = [], [], []
    chunks = ["The total is $1,200.", "50, and we can meet at 3:",
              "30 tomorrow. ", "Does that work for you?"]
    phrases = ["The total is $1,200.50, and we can meet at 3:30 tomorrow.",
               "Does that work for you?"]
    audio = b"\x2a" * 160

    def provider(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=audio)

    class Conversation:
        async def reply(self, *args, **kwargs):
            for chunk in chunks:
                yield chunk

    class FailedConversation:
        async def reply(self, *args, **kwargs):
            yield phrases[0] + " "
            raise RuntimeError("generation failed")

    class TrackedSession(tts.SpeechSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sessions.append(self)

    with monkeypatch.context() as scenario:
        scenario.setattr(relay, "SpeechSession", TrackedSession)
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            options = dict(gemini_api_key="test-gemini", tts_api_key="test-key",
                           voice_id="voiceA", voice_model=LEGACY_MODEL)
            reply = await relay.speak_reply(http, Conversation(), on_text=deltas.append, **options)
            assert reply.text == "".join(chunks) and reply.phrases == phrases
            assert deltas == chunks and reply.audio == audio * 2
            assert [request["text"] for request in requests] == phrases
            assert "previous_text" not in requests[0]
            assert requests[1]["previous_text"] == phrases[0]
            assert len(sessions) == 1 and sessions[0]._closed
            assert reply.first_text_ms is not None and reply.first_audio_ms is not None

            text_only = await relay.speak_reply(http, Conversation(), gemini_api_key="test-gemini",
                                               voice_id="invalid voice ID")
            assert text_only.text == reply.text and text_only.phrases == phrases
            assert not text_only.audio and text_only.first_audio_ms is None
            assert len(requests) == 2 and len(sessions) == 1

            with pytest.raises(RuntimeError, match="generation failed"):
                await relay.speak_reply(http, FailedConversation(), **options)
            assert len(sessions) == 2 and sessions[1]._closed
            assert not sessions[1]._streams and not sessions[1]._sockets


def test_dialogue_and_legacy_speech_streams_preserve_protocol_and_cancel_cleanly(monkeypatch):
    """A local dialogue provider covers delivery, completion and interruption."""
    async def run():
        requests, connections, urls = [], [], []
        cancelled_closed = asyncio.Event()
        cancel_waiting = asyncio.Event()
        audio = b"\x2a" * 9001

        def legacy(request):
            body = json.loads(request.content)
            requests.append(body)
            assert request.headers["xi-api-key"] == "test-key"
            assert request.url.params["output_format"] == "ulaw_8000"
            return httpx.Response(200, content=audio)

        async def dialogue(socket):
            assert socket.request.headers["xi-api-key"] == "test-key"
            assert parse_qs(urlsplit(socket.request.path).query) == {
                "model_id": [LATEST_REALTIME_MODEL], "output_format": ["ulaw_8000"]}
            initial = json.loads(await socket.recv())
            utterance = json.loads(await socket.recv())
            closing = json.loads(await socket.recv())
            connections.append((initial, utterance, closing))
            assert initial == {"voices": ["voiceA"], "voice_settings": {"stability": 0.5}}
            assert closing == {"close_socket": True}
            assert "test-key" not in json.dumps((initial, utterance, closing))
            item = utterance["inputs"][0]
            assert item["voice_id"] == "voiceA" and item["new_turn"] is False
            if item["text"].endswith("Reject this phrase."):
                await socket.send(json.dumps({"error": "rejected", "message": "Bad test-key", "code": 1008}))
                return
            if item["text"].endswith("Invalid audio."):
                await socket.send(json.dumps({"audio": "not base64!"}))
                return
            if item["text"].endswith("Provider never finishes."):
                await socket.wait_closed()
                return
            if item["text"].endswith("Cancel this phrase."):
                await socket.send(json.dumps({"audio": base64.b64encode(b"\x10").decode()}))
                cancel_waiting.set()
                await socket.wait_closed()
                cancelled_closed.set()
                return
            await socket.send(json.dumps({"audio": base64.b64encode(audio).decode()}))
            # A turn marker must not discard the final frame's remaining audio.
            await socket.send(json.dumps({"is_final_audio_for_turn": True}))
            await socket.send(json.dumps({"audio": base64.b64encode(b"\x11").decode(), "is_final": True}))

        async with serve(dialogue, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            def local_connect(uri, **options):
                urls.append(uri)
                parsed = urlsplit(uri)
                assert parsed.scheme == "wss" and parsed.netloc == "api.elevenlabs.io"
                assert parsed.path == "/v1/text-to-dialogue/stream-input"
                return real_connect(f"ws://127.0.0.1:{port}{parsed.path}?{parsed.query}",
                                    **options, proxy=None)

            monkeypatch.setattr(tts, "connect", local_connect)
            async with httpx.AsyncClient(transport=httpx.MockTransport(legacy)) as http:
                async with tts.SpeechSession(http, "test-key", "voiceA", model=LEGACY_MODEL) as session:
                    first = [piece async for piece in session.speech_bytes("First complete phrase.")]
                    second = [piece async for piece in session.speech_bytes("Second complete phrase.")]
                    assert b"".join(first) == b"".join(second) == audio
                    assert max(map(len, first + second)) <= 8000
                assert requests[0] == {
                    "text": "First complete phrase.", "model_id": LEGACY_MODEL,
                    "voice_settings": DEFAULT_DELIVERY.settings_for(LEGACY_MODEL)}
                assert requests[1]["previous_text"] == "First complete phrase."
                assert not urls

                async with tts.SpeechSession(http, "test-key", "voiceA") as session:
                    phrase = "Short reply."
                    pieces = [piece async for piece in session.speech_bytes(phrase)]
                    assert b"".join(pieces) == audio + b"\x11"
                    assert max(map(len, pieces)) <= 8000
                    assert connections[-1][1]["inputs"][0]["text"] == DEFAULT_DELIVERY.text_for(
                        phrase, LATEST_REALTIME_MODEL)
                    # No fallback request to the REST transport is allowed.
                    assert len(requests) == 2
                    with pytest.raises(tts.TTSError, match="rejected") as rejected:
                        await tts.speech(http, "test-key", "voiceA", "Reject this phrase.")
                    assert "test-key" not in str(rejected.value)
                    with pytest.raises(tts.TTSError, match="invalid encoded audio"):
                        await tts.speech(http, "test-key", "voiceA", "Invalid audio.")
                    with pytest.raises(tts.TTSError, match="timed out"):
                        await tts.speech(http, "test-key", "voiceA", "Provider never finishes.",
                                         timeout=0.05)

                    pending = session.speech_bytes("Cancel this phrase.")
                    assert await anext(pending) == b"\x10"
                    reader = asyncio.create_task(anext(pending))
                    await asyncio.wait_for(cancel_waiting.wait(), 1)
                    reader.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await reader
                    await pending.aclose()
                    await asyncio.wait_for(cancelled_closed.wait(), 1)
                    assert not session._sockets
                    assert session._previous_text == phrase
                with pytest.raises(tts.TTSError, match="closed"):
                    await anext(session.speech_bytes("No automatic redial."))

                # Runtime primes a stream in a prefetch task, then resumes it
                # from the playback task. A timeout spanning yield would cancel
                # this parked original reader during the playback pause.
                async with tts.SpeechSession(http, "test-key", "voiceA", timeout=0.1) as session:
                    crossing = session.speech_bytes("Cross task playback.")
                    prepared = asyncio.get_running_loop().create_future()
                    release_reader = asyncio.Event()

                    async def prepare():
                        prepared.set_result(await anext(crossing))
                        await release_reader.wait()

                    priming = asyncio.create_task(prepare())
                    try:
                        first = await asyncio.wait_for(prepared, 1)
                        await asyncio.sleep(0.15)
                        assert not priming.done(), "Playback must not cancel the task that primed audio"
                        remainder = [piece async for piece in crossing]
                        assert first + b"".join(remainder) == audio + b"\x11"
                    finally:
                        release_reader.set()
                        await asyncio.gather(priming, return_exceptions=True)
                        await crossing.aclose()
        await _verify_relay_continuity(monkeypatch)
    asyncio.run(run())
