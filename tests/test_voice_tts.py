"""Network-free ElevenLabs checks: streamed speech, enrollment, and validation.

The multipart assertions matter: HTTPX must build the boundary itself and the
sample field must stay ``files``, matching the provider's own client.
"""

import asyncio
import json

import httpx
import pytest

from test_voice_agent import FakeGemini, frame, text_event
from voice_stack.agent import Conversation, SentenceBuffer
from voice_stack.tts import (TTSError, create_clone, list_voices, speech, speech_bytes)

KEY = "unit-test-elevenlabs-key"
GEMINI_KEY = "unit-test-gemini-key"
VOICE = "EXAVITQu4vr4xnSDxMaL"
MODEL = "eleven_flash_v2_5"
ADD_VOICE_URL = "https://api.elevenlabs.io/v1/voices/add"
VOICES_URL = "https://api.elevenlabs.io/v1/voices"


class FakeSpeech:
    """A stand-in streaming endpoint that records what the adapter sent."""

    def __init__(self, *chunks, status=200, body=""):
        self.chunks = list(chunks)
        self.status = status
        self.body = body
        self.requests = []
        self.bodies = []

    async def handle(self, request):
        self.requests.append(request)
        self.bodies.append(await request.aread())
        return httpx.Response(self.status, content=self._body(),
                              headers={"content-type": "application/octet-stream"})

    async def _body(self):
        if self.chunks:
            for chunk in self.chunks:
                yield chunk
        elif self.body:
            yield self.body.encode()

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def json_body(self, index=0) -> dict:
        return json.loads(self.bodies[index])


class FakeVoices:
    """A synchronous stand-in for enrollment and listing."""

    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self.payload = payload
        self.text = text
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.payload is None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.payload)

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def body(self, index=0) -> bytes:
        # MockTransport reads the request before calling the handler.
        return self.requests[index].content


def run_speech(fake, *, api_key=KEY, voice_id=VOICE, text="Hello.", **options):
    async def run():
        async with fake.client() as http:
            return b"".join([chunk async for chunk in speech_bytes(
                http, api_key, voice_id, text, **options)])

    return asyncio.run(run())


def test_speech_streams_chunks_and_addresses_the_configured_voice():
    fake = FakeSpeech(b"\xff\xff", b"\x7f", b"\x00")
    audio = run_speech(fake)
    assert audio == b"\xff\xff\x7f\x00"
    request = fake.requests[0]
    assert str(request.url) == (f"https://api.elevenlabs.io/v1/text-to-speech/"
                                f"{VOICE}/stream?output_format=ulaw_8000")
    assert request.headers["xi-api-key"] == KEY
    assert request.headers["content-type"] == "application/json"
    assert fake.json_body() == {"text": "Hello.", "model_id": MODEL}
    assert KEY not in str(request.url)


def test_speech_returns_every_byte_for_offline_listening(tmp_path):
    fake = FakeSpeech(b"\xff" * 160)

    async def run():
        async with fake.client() as http:
            return await speech(http, KEY, VOICE, "One short phrase.")

    assert asyncio.run(run()) == b"\xff" * 160
    assert fake.json_body()["text"] == "One short phrase."


def test_speech_honours_an_explicit_model_and_output_format():
    fake = FakeSpeech(b"\x01")
    run_speech(fake, model="eleven_turbo_v2_5", output_format="pcm_16000")
    assert "output_format=pcm_16000" in str(fake.requests[0].url)
    assert fake.json_body()["model_id"] == "eleven_turbo_v2_5"


def test_speech_reports_a_provider_rejection_without_leaking_the_key():
    fake = FakeSpeech(status=401, body='{"detail":{"message":"invalid api key"}}')
    with pytest.raises(TTSError) as error:
        run_speech(fake)
    message = str(error.value)
    assert "HTTP 401" in message
    assert "invalid api key" in message
    assert KEY not in message


def test_a_missing_api_key_fails_before_any_request_is_made():
    fake = FakeSpeech(b"\xff")
    with pytest.raises(ValueError):
        run_speech(fake, api_key="")
    assert fake.requests == []


def test_a_transport_failure_becomes_a_tts_error():
    def explode(request):
        raise httpx.ConnectError("no route to host", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(explode)) as http:
            return b"".join([chunk async for chunk in
                             speech_bytes(http, KEY, VOICE, "Hello.")])

    with pytest.raises(TTSError) as error:
        asyncio.run(run())
    assert "ConnectError" in str(error.value)


@pytest.mark.parametrize("options", [
    {"voice_id": "voice id with spaces"},
    {"voice_id": "../../etc/passwd"},
    {"voice_id": ""},
    {"model": "eleven flash v2.5"},
    {"model": ""},
    {"output_format": "wav_8000"},
    {"output_format": "ulaw_8000&callback=https://other.example"},
    {"text": ""},
    {"text": "   "},
    {"text": None},
    {"text": "x" * 2001},
])
def test_speech_rejects_an_unusable_argument_before_the_request(options):
    fake = FakeSpeech(b"\xff")
    with pytest.raises(ValueError):
        run_speech(fake, **options)
    assert fake.requests == []


def test_create_clone_posts_multipart_with_repeated_files(tmp_path):
    first = tmp_path / "owner-a.wav"
    first.write_bytes(b"RIFF-first-sample")
    second = tmp_path / "owner-b.m4a"
    second.write_bytes(b"second-sample")
    fake = FakeVoices(payload={"voice_id": "21m00Tcm4TlvDq8ikWAM",
                               "requires_verification": True})
    result = create_clone(KEY, "owner-voice", [first, second], transport=fake.transport)
    assert result["voice_id"] == "21m00Tcm4TlvDq8ikWAM"
    request = fake.requests[0]
    assert str(request.url) == ADD_VOICE_URL
    assert request.headers["xi-api-key"] == KEY
    # HTTPX must create the boundary; the sample field must stay ``files``.
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    payload = fake.body()
    assert payload.count(b'name="files"') == 2
    assert b'name="name"' in payload and b"owner-voice" in payload
    assert b"RIFF-first-sample" in payload and b"second-sample" in payload
    assert b"owner-b.m4a" in payload
    assert KEY.encode() not in payload


def test_create_clone_rejects_no_missing_or_empty_samples(tmp_path):
    with pytest.raises(ValueError):
        create_clone(KEY, "owner-voice", [], transport=FakeVoices().transport)
    missing = tmp_path / "not-recorded.wav"
    with pytest.raises(ValueError):
        create_clone(KEY, "owner-voice", [missing], transport=FakeVoices().transport)
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        create_clone(KEY, "owner-voice", [empty], transport=FakeVoices().transport)


@pytest.mark.parametrize("name", ["", "   ", "x" * 81, "two\nlines"])
def test_create_clone_rejects_an_unusable_name(tmp_path, name):
    sample = tmp_path / "owner.wav"
    sample.write_bytes(b"RIFF-sample")
    fake = FakeVoices(payload={"voice_id": "voice"})
    with pytest.raises(ValueError):
        create_clone(KEY, name, [sample], transport=fake.transport)
    assert fake.requests == []


def test_create_clone_requires_an_api_key_before_reading_samples(tmp_path):
    fake = FakeVoices(payload={"voice_id": "voice"})
    with pytest.raises(ValueError):
        create_clone("", "owner-voice", [tmp_path / "missing.wav"], transport=fake.transport)
    assert fake.requests == []


def test_create_clone_reports_a_failure_without_leaking_the_key(tmp_path):
    sample = tmp_path / "owner.wav"
    sample.write_bytes(b"RIFF-sample")
    fake = FakeVoices(status=422, text='{"detail":{"message":"file too short"}}')
    with pytest.raises(TTSError) as error:
        create_clone(KEY, "owner-voice", [sample], transport=fake.transport)
    assert "HTTP 422" in str(error.value)
    assert KEY not in str(error.value)


@pytest.mark.parametrize("fake", [
    FakeVoices(payload={"requires_verification": False}),
    FakeVoices(payload={"voice_id": ""}),
    FakeVoices(status=200, text="<html>not json</html>"),
])
def test_create_clone_rejects_a_response_without_a_usable_voice_id(tmp_path, fake):
    sample = tmp_path / "owner.wav"
    sample.write_bytes(b"RIFF-sample")
    with pytest.raises(TTSError):
        create_clone(KEY, "owner-voice", [sample], transport=fake.transport)


def test_list_voices_is_a_free_credential_check():
    fake = FakeVoices(payload={"voices": [
        {"voice_id": VOICE, "name": "Sarah", "category": "premade"},
        {"voice_id": "clone-id", "name": "owner-voice", "category": "cloned"},
        "not-a-dict",
    ]})
    voices = list_voices(KEY, transport=fake.transport)
    assert [voice["voice_id"] for voice in voices] == [VOICE, "clone-id"]
    request = fake.requests[0]
    assert str(request.url) == VOICES_URL
    assert request.headers["xi-api-key"] == KEY
    # A listing is a bodyless GET: it must not look like a synthesis request.
    assert "content-type" not in request.headers
    assert request.content == b""


def test_list_voices_reports_a_failure_and_an_unexpected_shape():
    with pytest.raises(TTSError) as error:
        list_voices(KEY, transport=FakeVoices(status=403, text="forbidden").transport)
    assert "HTTP 403" in str(error.value)
    assert KEY not in str(error.value)
    with pytest.raises(TTSError):
        list_voices(KEY, transport=FakeVoices(payload={"voices": "wrong"}).transport)
    with pytest.raises(TTSError):
        list_voices(KEY, transport=FakeVoices(status=200, text="not json").transport)
    with pytest.raises(ValueError):
        list_voices("", transport=FakeVoices(payload={"voices": []}).transport)


def test_the_offline_chain_turns_gemini_deltas_into_one_request_per_phrase():
    """The whole pipeline with no account: stream, split, then synthesize.

    The relay's contract is one bounded TTS request per phrase, so the phrase
    count, the phrase text, and the request count all have to agree.
    """
    gemini = FakeGemini(frame(text_event("We open at nine. ")),
                        frame(text_event("Anything else?", finish="STOP")))
    speech_fake = FakeSpeech(b"\xff" * 160)

    async def route(request):
        if request.url.host == "generativelanguage.googleapis.com":
            return await gemini.handle(request)
        return await speech_fake.handle(request)

    async def run():
        conversation = Conversation()
        conversation.add_remote("When do you open?")
        buffer = SentenceBuffer()
        phrases = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as http:
            async for delta in conversation.reply(http, GEMINI_KEY, model="gemini-3.8-flash"):
                phrases.extend(buffer.feed(delta))
            trailing = buffer.flush()
            if trailing:
                phrases.append(trailing)
            for phrase in phrases:
                async for _ in speech_bytes(http, KEY, VOICE, phrase):
                    pass
        return phrases, conversation

    phrases, conversation = asyncio.run(run())
    assert phrases == ["We open at nine.", "Anything else?"]
    # One synthesis request per phrase, in order, carrying exactly that phrase.
    assert [json.loads(body)["text"] for body in speech_fake.bodies] == phrases
    assert len(gemini.requests) == 1
    assert len(speech_fake.requests) == 2
    # The reply was recorded as provider history, ready for the next turn.
    assert [turn["role"] for turn in conversation.contents] == ["user", "model"]