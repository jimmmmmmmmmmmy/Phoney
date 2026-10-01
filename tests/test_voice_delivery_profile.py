"""A delivery change must actually replace cached speech, preserving words."""

import asyncio
from dataclasses import replace
import json

import httpx

from operator_service.controls import ClipLibrary
from voice_stack.delivery import LEGACY_MODEL, VoiceDelivery
from voice_stack.settings import VoiceSettings


def test_delivery_change_regenerates_cached_clip_without_rewriting_words(tmp_path):
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
