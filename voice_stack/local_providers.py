"""Local Phonon speech and Nemotron dialogue, with cloud fallback.

Phonon is a local HTTP TTS server. Nemotron is a local OpenAI-compatible chat
server. Phoney does not launch those processes and does not read the weight
files itself. When a local provider is selected but unreachable, and the
matching cloud credential is configured, the existing ElevenLabs or Gemini
adapter is used for that call.
"""

from __future__ import annotations

import array
import asyncio
import io
import json
import wave
from contextlib import asynccontextmanager

import httpx

from .audio import SAMPLE_RATE, pcm16_to_ulaw
from .tts import SpeechSession

CONNECT_SECONDS = 5.0
HEALTH_SECONDS = 1.5


class LocalProviderError(RuntimeError):
    """A local Phonon or Nemotron request failed before cloud fallback."""

    def __init__(self, message, *, provider="local"):
        super().__init__(message)
        self.provider = provider


def api_root(base_url: str) -> str:
    """Origin used for ``/health`` and ``/v1/...``. A trailing ``/v1`` is stripped."""
    root = str(base_url).rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root


async def local_reachable(http: httpx.AsyncClient, base_url: str) -> bool:
    """True when the local server answers. Connection failure is unhealthy.

    HTTP 404 still counts: the process is up and may only implement the POST
    path. HTTP 5xx and transport errors do not.
    """
    root = api_root(base_url)
    for path in ("/health", "/v1/models"):
        try:
            response = await http.get(root + path, timeout=HEALTH_SECONDS)
        except httpx.HTTPError:
            continue
        if response.status_code < 500:
            return True
    return False


def _resample_pcm16(data: bytes, src_rate: int, dst_rate: int = SAMPLE_RATE) -> bytes:
    if src_rate == dst_rate:
        return data
    if src_rate < 1000 or src_rate > 96000:
        raise LocalProviderError("Phonon sample rate is not usable for phone audio.", provider="phonon")
    samples = array.array("h")
    samples.frombytes(data)
    if not samples:
        raise LocalProviderError("Phonon returned no audio.", provider="phonon")
    out_len = max(1, int(len(samples) * dst_rate / src_rate))
    out = array.array("h")
    last = len(samples) - 1
    for index in range(out_len):
        position = index * src_rate / dst_rate
        left = min(int(position), last)
        right = min(left + 1, last)
        frac = position - left
        out.append(int(samples[left] + (samples[right] - samples[left]) * frac))
    return out.tobytes()


def _wav_to_ulaw(data: bytes) -> bytes:
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            channels = audio.getnchannels()
            rate = audio.getframerate()
            width = audio.getsampwidth()
            frames = audio.readframes(audio.getnframes())
    except wave.Error as exc:
        raise LocalProviderError("Phonon returned an unreadable WAV.", provider="phonon") from exc
    if width != 2 or channels < 1:
        raise LocalProviderError("Phonon WAV must be 16-bit PCM.", provider="phonon")
    if channels > 1:
        samples = array.array("h")
        samples.frombytes(frames)
        frames = array.array("h", samples[::channels]).tobytes()
    return pcm16_to_ulaw(_resample_pcm16(frames, rate))


def audio_to_ulaw(content: bytes, content_type: str) -> bytes:
    """Turn a local TTS body into 8 kHz mu-law for Twilio."""
    if not content:
        raise LocalProviderError("Phonon returned no audio.", provider="phonon")
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    if content[:4] == b"RIFF" or kind in {"audio/wav", "audio/x-wav", "audio/wave"}:
        return _wav_to_ulaw(content)
    if kind == "application/json":
        try:
            payload = json.loads(content)
        except ValueError as exc:
            raise LocalProviderError("Phonon returned malformed JSON.", provider="phonon") from exc
        encoded = payload.get("audio") if isinstance(payload, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise LocalProviderError("Phonon JSON did not include audio.", provider="phonon")
        import base64
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise LocalProviderError("Phonon JSON audio was not base64.", provider="phonon") from exc
        nested = str(payload.get("content_type") or "audio/basic")
        return audio_to_ulaw(raw, nested)
    if kind in {"audio/l16", "audio/pcm"}:
        rate = 24000 if "rate=24000" in (content_type or "") else SAMPLE_RATE
        return pcm16_to_ulaw(_resample_pcm16(content, rate))
    # Requested response_format is ulaw_8000. Unlabeled bytes are that format.
    return content


async def phonon_ulaw(http: httpx.AsyncClient, voice, text: str, *, timeout: float) -> bytes:
    """One phrase from the local Phonon server, as 8 kHz mu-law."""
    root = api_root(voice.phonon_base_url)
    try:
        response = await http.post(
            root + "/v1/audio/speech",
            json={
                "model": voice.phonon_model,
                "voice": voice.phonon_voice or voice.phonon_model,
                "input": text,
                "response_format": "ulaw_8000",
            },
            timeout=httpx.Timeout(timeout, connect=CONNECT_SECONDS),
        )
    except httpx.HTTPError as exc:
        raise LocalProviderError(f"Phonon request failed: {type(exc).__name__}", provider="phonon") from exc
    if response.status_code != 200:
        raise LocalProviderError(f"Phonon failed with HTTP {response.status_code}", provider="phonon")
    return audio_to_ulaw(response.content, response.headers.get("content-type", ""))


def _chunks(audio: bytes, size: int = 8000):
    for start in range(0, len(audio), size):
        yield audio[start:start + size]


class _PhononSession:
    """SpeechSession-shaped local TTS. Falls back once per reply, not mid-phrase."""

    def __init__(self, http, voice, fallback: SpeechSession | None, timeout: float):
        self.http = http
        self.voice = voice
        self.fallback = fallback
        self.timeout = timeout
        self._use_fallback = False
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()

    async def speech_bytes(self, text: str, *, previous_text=None):
        if self._closed:
            raise LocalProviderError("Phonon session is closed.", provider="phonon")
        if self._use_fallback:
            async for chunk in self.fallback.speech_bytes(text, previous_text=previous_text):
                yield chunk
            return
        try:
            audio = await phonon_ulaw(self.http, self.voice, text, timeout=self.timeout)
        except (LocalProviderError, httpx.HTTPError):
            if self.fallback is None:
                raise
            self._use_fallback = True
            async for chunk in self.fallback.speech_bytes(text, previous_text=previous_text):
                yield chunk
            return
        if not audio:
            raise LocalProviderError("Phonon returned no audio.", provider="phonon")
        for chunk in _chunks(audio):
            yield chunk

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self.fallback is not None:
            await self.fallback.close()


def _elevenlabs_session(http, voice, voice_id: str) -> SpeechSession:
    return SpeechSession(
        http, voice.elevenlabs_api_key, voice_id,
        model=voice.elevenlabs_model, output_format="ulaw_8000",
        timeout=voice.request_timeout, delivery=voice.delivery,
    )


@asynccontextmanager
async def open_speech_session(http, voice, voice_id: str):
    """Phonon when selected and healthy; otherwise the existing ElevenLabs session."""
    if getattr(voice, "tts_provider", "elevenlabs") != "phonon":
        async with _elevenlabs_session(http, voice, voice_id) as session:
            yield session
        return
    fallback = None
    if voice.elevenlabs_api_key and voice.elevenlabs_voice_id:
        fallback = _elevenlabs_session(http, voice, voice.elevenlabs_voice_id)
    reachable = await local_reachable(http, voice.phonon_base_url)
    if not reachable and fallback is not None:
        async with fallback as session:
            yield session
        return
    if not reachable:
        raise LocalProviderError("Phonon server is unreachable.", provider="phonon")
    async with _PhononSession(http, voice, fallback, voice.request_timeout) as session:
        yield session


async def synthesize_ulaw(http, voice, voice_id: str, text: str, *, timeout: float) -> bytes:
    """One complete phrase, used for fixed announcements."""
    if getattr(voice, "tts_provider", "elevenlabs") != "phonon":
        from .tts import speech
        return await speech(
            http, voice.elevenlabs_api_key, voice_id, text,
            model=voice.elevenlabs_model, output_format="ulaw_8000",
            delivery=voice.delivery, timeout=timeout)
    try:
        if not await local_reachable(http, voice.phonon_base_url):
            raise LocalProviderError("Phonon server is unreachable.", provider="phonon")
        return await phonon_ulaw(http, voice, text, timeout=timeout)
    except (LocalProviderError, httpx.HTTPError):
        if not (voice.elevenlabs_api_key and voice.elevenlabs_voice_id):
            raise
        from .tts import speech
        return await speech(
            http, voice.elevenlabs_api_key, voice.elevenlabs_voice_id, text,
            model=voice.elevenlabs_model, output_format="ulaw_8000",
            delivery=voice.delivery, timeout=timeout)


def _messages(system: str, contents: list[dict]) -> list[dict]:
    messages = [{"role": "system", "content": system}]
    for item in contents:
        parts = item.get("parts") if isinstance(item, dict) else None
        if not isinstance(parts, list):
            continue
        text = "".join(
            part.get("text", "") for part in parts
            if isinstance(part, dict) and part.get("text") and not part.get("thought"))
        if not text:
            continue
        role = "assistant" if item.get("role") == "model" else "user"
        messages.append({"role": role, "content": text})
    return messages


def _delta_text(event: dict) -> str:
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    choice = choices[0]
    delta = choice.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
        return delta["content"]
    message = choice.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return ""


async def nemotron_events(http, voice, system: str, contents: list[dict], *,
                          timeout: float, first_text_timeout: float | None):
    """Stream one Nemotron reply as ``{"kind": "text"}`` events."""
    root = api_root(voice.nemotron_base_url)
    headers = {"content-type": "application/json"}
    if voice.nemotron_api_key:
        headers["authorization"] = "Bearer " + voice.nemotron_api_key
    body = {
        "model": voice.nemotron_model,
        "messages": _messages(system, contents),
        "stream": True,
        "max_tokens": voice.max_reply_tokens,
        "temperature": 0.3,
    }
    spoke = False
    try:
        async with asyncio.timeout(timeout):
            async with asyncio.timeout(first_text_timeout) as first_text_guard:
                async with http.stream(
                    "POST", root + "/v1/chat/completions", headers=headers, json=body,
                    timeout=httpx.Timeout(10.0, connect=CONNECT_SECONDS),
                ) as response:
                    if response.status_code != 200:
                        raise LocalProviderError(
                            f"Nemotron failed with HTTP {response.status_code}", provider="nemotron")
                    content_type = response.headers.get("content-type", "")
                    if "application/json" in content_type and "text/event-stream" not in content_type:
                        payload = json.loads(await response.aread())
                        text = _delta_text(payload)
                        if not text:
                            raise LocalProviderError("Nemotron returned no text.", provider="nemotron")
                        yield {"kind": "text", "text": text}
                        return
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            event = json.loads(data)
                        except ValueError as exc:
                            raise LocalProviderError("Nemotron returned malformed JSON.", provider="nemotron") from exc
                        text = _delta_text(event)
                        if text:
                            if not spoke:
                                spoke = True
                                first_text_guard.reschedule(None)
                            yield {"kind": "text", "text": text}
    except TimeoutError as exc:
        raise LocalProviderError("Nemotron timed out.", provider="nemotron") from exc
    if not spoke:
        raise LocalProviderError("Nemotron returned no text.", provider="nemotron")


async def iter_reply_events(http, voice, system, contents, *, model, max_output_tokens,
                            timeout, first_text_timeout, retry_first_text_timeout, trace):
    """Nemotron when selected and healthy, otherwise the existing Gemini stream."""
    from .agent import reply_events_with_retry as gemini_events

    use_nemotron = getattr(voice, "llm_provider", "gemini") == "nemotron"
    if use_nemotron and voice.gemini_api_key and not await local_reachable(http, voice.nemotron_base_url):
        yield {"kind": "retry", "reason": "nemotron-unreachable"}
        use_nemotron = False
    if not use_nemotron:
        async for event in gemini_events(
            http, voice.gemini_api_key, system, contents, model=model,
            max_output_tokens=max_output_tokens, timeout=timeout,
            first_text_timeout=first_text_timeout,
            retry_first_text_timeout=retry_first_text_timeout, trace=trace):
            yield event
        return
    produced = False
    try:
        async for event in nemotron_events(
            http, voice, system, contents, timeout=timeout,
            first_text_timeout=first_text_timeout):
            if event.get("kind") == "text":
                produced = True
            yield event
    except (LocalProviderError, httpx.HTTPError, TimeoutError) as exc:
        if produced or not voice.gemini_api_key:
            raise
        yield {"kind": "retry", "reason": "nemotron-" + type(exc).__name__}
        async for event in gemini_events(
            http, voice.gemini_api_key, system, contents, model=model,
            max_output_tokens=max_output_tokens, timeout=timeout,
            first_text_timeout=first_text_timeout,
            retry_first_text_timeout=retry_first_text_timeout, trace=trace):
            yield event
