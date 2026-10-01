"""Compose one streamed Gemini reply into bounded ElevenLabs synthesis requests.

``agent`` owns the provider conversation and ``tts`` owns synthesis; this module
is the only place that knows the two are used together. Each finished phrase
becomes exactly one synthesis request, and text is handed back while the reply
is still being generated rather than after it ends.

The contract lives here so the typed conversation and the in-call relay speak
through one implementation: two copies of this loop would be free to disagree
about when a phrase is speakable, and the audio would drift from the answer it
claims to be.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
import time

import httpx

from .agent import DEFAULT_MODEL, Conversation, SentenceBuffer
from .delivery import DEFAULT_DELIVERY, VoiceDelivery
from .settings import TWILIO_FORMAT
from .tts import DEFAULT_MODEL as DEFAULT_VOICE_MODEL
from .tts import SpeechSession


@dataclass
class SpokenReply:
    """One reply: the text it produced, the phrases spoken, and the audio."""

    text: str = ""
    phrases: list[str] = field(default_factory=list)
    audio: bytes = b""
    first_text_ms: int | None = None
    first_audio_ms: int | None = None
    total_ms: int = 0


async def speak_reply(http: httpx.AsyncClient, conversation: Conversation, *,
                      gemini_api_key: str, model: str = DEFAULT_MODEL,
                      max_output_tokens: int = 2048, request_timeout: float = 20.0,
                      tts_api_key: str = "", voice_id: str = "",
                      voice_model: str = DEFAULT_VOICE_MODEL,
                      output_format: str = TWILIO_FORMAT,
                      delivery: VoiceDelivery = DEFAULT_DELIVERY,
                      on_text=None) -> SpokenReply:
    """Stream one reply, synthesising each finished phrase as it appears.

    The synthesis key and voice are both optional: with either absent the reply
    is still recorded and returned as text, which is all a text-only run wants.
    ``on_text`` receives each delta as it arrives, so a caller can print or
    forward the reply while it is still being generated.

    A generation that fails mid-stream raises before any audio is returned, and
    ``Conversation.reply`` records nothing in that case: a partial reply must
    never be replayed to the provider as though the model had finished it.
    """
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    reply = SpokenReply()
    buffer = SentenceBuffer()
    audio = bytearray()
    speaking = bool(tts_api_key and voice_id)

    async with AsyncExitStack() as cleanup:
        synthesis = None
        if speaking:
            # One reply owns its context and provider streams. Legacy models
            # inherit completed phrase text just as they do in the live relay.
            synthesis = await cleanup.enter_async_context(SpeechSession(
                http, tts_api_key, voice_id, model=voice_model,
                output_format=output_format, delivery=delivery,
                timeout=request_timeout))

        async def render(phrase: str) -> None:
            async for chunk in synthesis.speech_bytes(phrase):
                audio.extend(chunk)
            if reply.first_audio_ms is None:
                reply.first_audio_ms = elapsed()

        async for delta in conversation.reply(http, gemini_api_key, model=model,
                                              max_output_tokens=max_output_tokens,
                                              timeout=request_timeout):
            if reply.first_text_ms is None:
                reply.first_text_ms = elapsed()
            reply.text += delta
            if on_text is not None:
                on_text(delta)
            for phrase in buffer.feed(delta):
                reply.phrases.append(phrase)
                if speaking:
                    await render(phrase)
        trailing = buffer.flush()
        if trailing:
            reply.phrases.append(trailing)
            if speaking:
                await render(trailing)
    reply.audio = bytes(audio)
    reply.total_ms = elapsed()
    return reply
