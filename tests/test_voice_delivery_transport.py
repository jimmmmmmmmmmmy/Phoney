"""Exercise the provider wire protocol locally; never synthesize paid audio."""

import asyncio
import base64
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from websockets.asyncio.client import connect as real_connect
from websockets.asyncio.server import serve

from voice_stack import tts
from voice_stack.delivery import DEFAULT_DELIVERY, LEGACY_MODEL, LATEST_REALTIME_MODEL


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
    asyncio.run(run())
