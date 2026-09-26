"""The relay contract: one bounded synthesis request per finished phrase.

``speak_reply`` is the only place where a streamed reply becomes speech, so
these checks pin the phrase list, the request order and the returned text to the
same answer. Both providers are ``MockTransport`` endpoints, so the real
streaming code paths run without a credential and without a request leaving the
process.
"""

import asyncio
import json

import httpx
import pytest

from test_voice_agent import FakeGemini, frame, text_event
from test_voice_tts import FakeSpeech
from voice_stack.agent import Conversation, GeminiError
from voice_stack.relay import SpokenReply, speak_reply

GEMINI_KEY = "unit-test-gemini-key"
ELEVEN_KEY = "unit-test-elevenlabs-key"
VOICE = "EXAVITQu4vr4xnSDxMaL"


def routed(gemini, speech_fake) -> httpx.MockTransport:
    """One transport for both providers, so a test needs no credential."""

    async def route(request):
        if request.url.host == "generativelanguage.googleapis.com":
            return await gemini.handle(request)
        return await speech_fake.handle(request)

    return httpx.MockTransport(route)


def opening() -> Conversation:
    """A conversation with one attributed question, as a call would have."""
    conversation = Conversation.handoff([("owner", "Hold the line politely.")],
                                        goal="Reply in one short sentence.")
    conversation.add_remote("When do you open?")
    return conversation


def run(conversation, gemini, speech_fake, **options) -> SpokenReply:
    """Drive one reply through both fakes, with the voice pair on by default."""
    options.setdefault("gemini_api_key", GEMINI_KEY)
    options.setdefault("tts_api_key", ELEVEN_KEY)
    options.setdefault("voice_id", VOICE)

    async def speak() -> SpokenReply:
        async with httpx.AsyncClient(transport=routed(gemini, speech_fake)) as http:
            return await speak_reply(http, conversation, **options)

    return asyncio.run(speak())


def test_one_synthesis_request_per_phrase_in_order_and_twilio_ready():
    gemini = FakeGemini(frame(text_event("We open at nine. ")),
                        frame(text_event("Anything else?", finish="STOP")))
    speech_fake = FakeSpeech(b"\xff" * 160)

    reply = run(opening(), gemini, speech_fake)

    assert reply.phrases == ["We open at nine.", "Anything else?"]
    assert reply.text == "We open at nine. Anything else?"
    assert [json.loads(body)["text"] for body in speech_fake.bodies] == reply.phrases
    assert len(gemini.requests) == 1
    # mu-law at 8 kHz: the bytes go straight into Twilio's media payload.
    assert all("output_format=ulaw_8000" in str(request.url)
               for request in speech_fake.requests)
    assert reply.audio == b"\xff" * 320


def test_on_text_receives_every_delta_in_order():
    gemini = FakeGemini(frame(text_event("One. ")),
                        frame(text_event("Two.", finish="STOP")))
    seen: list[str] = []

    reply = run(opening(), gemini, FakeSpeech(b"\x00"), on_text=seen.append)

    assert seen == ["One. ", "Two."]
    assert "".join(seen) == reply.text


@pytest.mark.parametrize("overrides", [{"tts_api_key": ""}, {"voice_id": ""}])
def test_either_half_of_the_voice_pair_missing_keeps_the_reply_text_only(overrides):
    gemini = FakeGemini(frame(text_event("Nine.", finish="STOP")))
    speech_fake = FakeSpeech(b"\xff" * 160)

    reply = run(opening(), gemini, speech_fake, **overrides)

    assert speech_fake.requests == []
    assert reply.audio == b"" and reply.first_audio_ms is None
    assert reply.phrases == ["Nine."] and reply.text == "Nine."


def test_a_long_unpunctuated_reply_is_still_released_as_phrases():
    """The buffer's limit is the reason an unpunctuated reply can still speak."""
    said = "word " * 40
    gemini = FakeGemini(frame(text_event(said, finish="STOP")))
    speech_fake = FakeSpeech(b"\xff" * 8)

    reply = run(opening(), gemini, speech_fake)

    # Phrases are trimmed for speech; the reply text keeps every character.
    assert reply.text == said
    assert len(reply.phrases) > 1
    assert sum(len(phrase.split()) for phrase in reply.phrases) == 40
    assert {word for phrase in reply.phrases for word in phrase.split()} == {"word"}
    assert len(speech_fake.requests) == len(reply.phrases)


def test_an_incomplete_generation_raises_and_records_nothing():
    # No finishReason: a partial reply must never be replayed as history.
    gemini = FakeGemini(frame(text_event("Half a sentence")))
    speech_fake = FakeSpeech(b"\xff")
    conversation = opening()
    before = list(conversation.contents)

    with pytest.raises(GeminiError):
        run(conversation, gemini, speech_fake)

    assert conversation.contents == before
    # The fragment never reached a phrase boundary, so nothing was synthesised.
    assert speech_fake.requests == []


def test_a_completed_reply_is_recorded_so_the_next_turn_can_use_it():
    gemini = FakeGemini(frame(text_event("Nine.", finish="STOP")))
    conversation = opening()
    before = list(conversation.contents)

    run(conversation, gemini, FakeSpeech(b"\xff"))

    assert len(conversation.contents) == len(before) + 1
    assert conversation.contents[-1] == {"role": "model", "parts": [{"text": "Nine."}]}


def test_timings_describe_one_reply_in_order():
    gemini = FakeGemini(frame(text_event("Nine.", finish="STOP")))

    reply = run(opening(), gemini, FakeSpeech(b"\xff" * 160))

    assert reply.first_text_ms is not None and reply.first_audio_ms is not None
    assert 0 <= reply.first_text_ms <= reply.first_audio_ms <= reply.total_ms


def test_the_voice_override_reaches_the_synthesis_url():
    gemini = FakeGemini(frame(text_event("Nine.", finish="STOP")))
    speech_fake = FakeSpeech(b"\xff")

    run(opening(), gemini, speech_fake, voice_id="overrideVoiceId123")

    assert "/text-to-speech/overrideVoiceId123/stream" in str(speech_fake.requests[0].url)