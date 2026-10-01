"""Shared delivery preserves spoken text, phrase boundaries, and cache identity."""

import asyncio
from dataclasses import replace
import json

import httpx

from operator_service.controls import ClipLibrary
from voice_stack.agent import SentenceBuffer
from voice_stack.delivery import LEGACY_MODEL, VoiceDelivery
from voice_stack.settings import VoiceSettings


def _phrases_for(chunks):
    buffer = SentenceBuffer()
    phrases = []
    for chunk in chunks:
        phrases.extend(buffer.feed(chunk))
    if trailing := buffer.flush():
        phrases.append(trailing)
    assert buffer.flush() is None
    return phrases


def _check_spoken_phrase_boundaries():
    question = (
        "Would tomorrow afternoon work for you if we take a little extra time "
        "to review the available options, check the reservation details, and "
        "make sure the appointment fits your schedule?"
    )
    assert 150 <= len(question) <= 250
    examples = [
        (
            "The total is $1,200.50. Could you confirm the payment?",
            ["The total is $1,200.50.", "Could you confirm the payment?"],
        ),
        (
            "Please call Dr. Rivera at 3:30 p.m. tomorrow. Mr. Singh has the address.",
            ["Please call Dr. Rivera at 3:30 p.m. tomorrow.", "Mr. Singh has the address."],
        ),
        (
            "A. Rivera can help with the U.S. office, e.g. the morning appointment. "
            "Please arrive at 9:15 a.m. sharp.",
            ["A. Rivera can help with the U.S. office, e.g. the morning appointment.",
             "Please arrive at 9:15 a.m. sharp."],
        ),
        (
            "Use https://example.com/bookings?time=3:30 and email jane.doe@example.com "
            "before confirming the $1,200.50 payment.",
            ["Use https://example.com/bookings?time=3:30 and email jane.doe@example.com "
             "before confirming the $1,200.50 payment."],
        ),
        (question, [question]),
        (
            "Here's the plan: we can check the time; the reservation can wait.",
            ["Here's the plan: we can check the time; the reservation can wait."],
        ),
        (
            "She said, “That works.” Then we agreed. Well... that sounds good!",
            ["She said, “That works.”", "Then we agreed.", "Well... that sounds good!"],
        ),
        (
            "First request is complete. Next, can you check the time? Yes, that works!",
            ["First request is complete.", "Next, can you check the time?", "Yes, that works!"],
        ),
    ]
    cap_body = " ".join(["detail"] * 42) + " later"
    assert len(cap_body) == 299
    for mark in ".?!":
        at_cap = cap_body + mark
        examples.append((at_cap + " Next sentence.", [at_cap, "Next sentence."]))
    quoted_at_cap = "“" + " ".join(["detail"] * 42) + " now?”"
    assert len(quoted_at_cap) == 300
    examples.append((quoted_at_cap + " That works.", [quoted_at_cap, "That works."]))
    for text, expected in examples:
        assert _phrases_for([text]) == expected
        # Provider deltas can end on any character, including decimal dots,
        # abbreviation dots, URL punctuation, and closing quotation marks.
        assert _phrases_for(list(text)) == expected
        for split in range(len(text) + 1):
            assert _phrases_for([text[:split], text[split:]]) == expected
        assert " ".join(expected).split() == text.split()

    # A punctuation token does not establish a boundary before the provider
    # has supplied the rest of a numeric token or a closing quotation mark.
    buffer = SentenceBuffer()
    assert buffer.feed("The total is $1,200") == []
    assert buffer.feed(".") == []
    assert buffer.feed("50") == []
    assert buffer.feed(".") == []
    assert buffer.feed(" ") == ["The total is $1,200.50."]
    assert buffer.flush() is None

    lead = "We can review " + "the reservation details at a comfortable pace " * 4
    lead = lead.strip() + ";"
    tail = " ".join(f"option{number}" for number in range(100))
    long_examples = [(lead + " " + tail, lead), (tail, None)]
    for mark in ",;:":
        at_cap = cap_body + mark
        long_examples.append((at_cap + " " + tail, at_cap))
    # The decimal dot itself can land exactly on the length limit: chunking
    # must still leave the numeric token whole when choosing a word boundary.
    price_at_cap = "detail " * 40 + "The total is $1,200.50. Next sentence."
    assert price_at_cap.index(".") == 299
    long_examples.append((price_at_cap, None))
    for text, preferred_clause in long_examples:
        assert len(text) > 300
        expected = _phrases_for([text])
        assert len(expected) > 1
        assert all(len(phrase) <= 300 for phrase in expected)
        assert " ".join(expected).split() == text.split()
        if preferred_clause:
            assert expected[0] == preferred_clause
        assert _phrases_for(list(text)) == expected
        for split in range(len(text) + 1):
            assert _phrases_for([text[:split], text[split:]]) == expected
    assert "$1,200.50." in " ".join(_phrases_for([price_at_cap])).split()

    # A pathological provider token has no safe word boundary. Its fallback
    # must remain bounded and preserve every character, including punctuation.
    unbroken = "https://example.com/" + "a" * 1_200 + "?time=3:30"
    expected = _phrases_for([unbroken])
    assert len(expected) > 1
    assert all(len(phrase) <= 300 for phrase in expected)
    assert "".join(expected) == unbroken
    assert _phrases_for(list(unbroken)) == expected
    for split in range(len(unbroken) + 1):
        assert _phrases_for([unbroken[:split], unbroken[split:]]) == expected


def test_shared_delivery_preserves_phrase_text_and_regenerates_changed_cached_clips(tmp_path):
    _check_spoken_phrase_boundaries()
    requests = []

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, content=b"\xff" * 800)

    voice = VoiceSettings(elevenlabs_api_key="test", elevenlabs_voice_id="voiceA",
                          elevenlabs_model=LEGACY_MODEL, output_dir=str(tmp_path),
                          delivery=VoiceDelivery(stability=0.4))
    transport = httpx.MockTransport(provider)
    original = ClipLibrary(voice, transport=transport)
    text = "Hi, the owner can't answer right now. Please leave your message."

    async def scenario():
        first = await original.bytes_for("greeting", text)
        assert await original.bytes_for("greeting", text) == first
        assert len(requests) == 1

        tuned_voice = replace(voice, delivery=VoiceDelivery(stability=0.35))
        tuned = ClipLibrary(tuned_voice, transport=transport)
        assert tuned.path("greeting", text) != original.path("greeting", text)
        assert not tuned.cached("greeting", text)
        await tuned.bytes_for("greeting", text)
        assert len(requests) == 2
        assert requests[0]["text"] == requests[1]["text"] == text
        assert requests[0]["voice_settings"]["stability"] == 0.4
        assert requests[1]["voice_settings"]["stability"] == 0.35
        assert requests[1]["voice_settings"]["style"] == 0

    asyncio.run(scenario())
