"""ElevenLabs cloned speech: streamed synthesis plus one-time voice enrollment.

Protocol: https://elevenlabs.io/docs/api-reference/text-to-speech/stream
``ulaw_8000`` is one byte per 8 kHz sample, so the returned bytes are already
Twilio-ready: base64 them into ``media.payload`` and never prepend a WAV
header. Validation reuses the patterns in ``settings`` so an adapter error and
a configuration error can never disagree about what is acceptable.

The voice is enrolled once, before any call, and reused by ID. Creating a
clone inside a media handler is not merely inefficient: it would add seconds
of latency and would enroll a voice from live phone audio.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

import httpx

from .settings import ELEVENLABS_MODEL as ELEVENLABS_MODEL_PATTERN
from .settings import OUTPUT_FORMATS, TWILIO_FORMAT
from .settings import VOICE_ID as VOICE_ID_PATTERN

TTS_BASE = "https://api.elevenlabs.io/v1/text-to-speech"
CLONE_URL = "https://api.elevenlabs.io/v1/voices/add"
VOICES_URL = "https://api.elevenlabs.io/v1/voices"
DEFAULT_MODEL = "eleven_flash_v2_5"
# One phrase, not one reply: the relay flushes around 120 characters, so this
# only ever rejects a caller that bypassed the sentence buffer.
MAX_TEXT_CHARS = 2000
MAX_NAME_CHARS = 80
CONNECT_SECONDS = 5.0
SPEECH_SECONDS = 30.0
CLONE_SECONDS = 120.0
EXCERPT_CHARS = 300


class TTSError(RuntimeError):
    """Speech or enrollment failed: transport, rejection, or a bad response."""

    def __init__(self, message, *, http_status=None):
        super().__init__(message)
        self.http_status = http_status


def _client(transport, timeout: float) -> httpx.Client:
    """One short-lived client; a supplied transport keeps tests off the network."""
    return httpx.Client(transport=transport,
                        timeout=httpx.Timeout(timeout, connect=CONNECT_SECONDS))


def _failure(action: str, status: int, body: str = "") -> TTSError:
    """A provider failure carrying status and message, never request headers."""
    detail = " ".join(str(body).split())[:EXCERPT_CHARS]
    return TTSError(f"{action} failed with HTTP {status}" + (f": {detail}" if detail else ""),
                    http_status=status)


def _checked_voice_id(voice_id) -> str:
    if not isinstance(voice_id, str) or not VOICE_ID_PATTERN.fullmatch(voice_id):
        raise ValueError("Use a voice identifier from your account.")
    return voice_id


def _checked_model(model) -> str:
    if not isinstance(model, str) or not ELEVENLABS_MODEL_PATTERN.fullmatch(model):
        raise ValueError("Use a documented ElevenLabs model identifier.")
    return model


def _checked_format(output_format) -> str:
    if output_format not in OUTPUT_FORMATS:
        raise ValueError("Use a documented ElevenLabs output format.")
    return output_format


def _checked_text(text) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Speech needs non-empty text.")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"A single phrase is limited to {MAX_TEXT_CHARS} characters.")
    return text


def _checked_name(name) -> str:
    name = str(name).strip()
    if not 1 <= len(name) <= MAX_NAME_CHARS:
        raise ValueError(f"A voice name must be 1 to {MAX_NAME_CHARS} characters.")
    if "\n" in name or "\r" in name:
        raise ValueError("A voice name cannot contain line breaks.")
    return name


def _checked_samples(sample_paths) -> list[Path]:
    """Resolve sample paths, rejecting anything that cannot be uploaded."""
    paths = []
    for raw in sample_paths or ():
        path = Path(raw)
        if not path.is_file():
            raise ValueError(f"Voice sample not found: {path}")
        if not path.stat().st_size:
            raise ValueError(f"Voice sample is empty: {path.name}")
        paths.append(path)
    if not paths:
        raise ValueError("Supply at least one voice sample.")
    return paths


async def speech_bytes(http: httpx.AsyncClient, api_key: str, voice_id: str, text: str, *,
                       model: str = DEFAULT_MODEL, output_format: str = TWILIO_FORMAT,
                       timeout: float = SPEECH_SECONDS):
    """Stream one phrase of cloned speech as raw audio bytes.

    Chunks are handed on exactly as received so the relay can start writing to
    Twilio before the utterance finishes. With ``ulaw_8000`` the bytes are
    μ-law, not PCM: do not label them as anything else or add a container.
    """
    if not api_key:
        raise ValueError("ELEVENLABS_API_KEY is required before generating speech.")
    voice_id = _checked_voice_id(voice_id)
    text = _checked_text(text)
    model = _checked_model(model)
    output_format = _checked_format(output_format)
    try:
        async with http.stream(
            "POST",
            f"{TTS_BASE}/{voice_id}/stream",
            params={"output_format": output_format},
            headers={"xi-api-key": api_key, "content-type": "application/json"},
            json={"text": text, "model_id": model},
            timeout=httpx.Timeout(timeout, connect=CONNECT_SECONDS),
        ) as response:
            if response.status_code != 200:
                raise _failure("Speech request", response.status_code,
                               (await response.aread()).decode("utf-8", "replace"))
            async for chunk in response.aiter_bytes():
                if chunk:
                    yield chunk
    except httpx.HTTPError as exc:
        # Cancellation is a BaseException and deliberately passes through.
        raise TTSError(f"Speech request failed: {type(exc).__name__}") from exc


async def speech(http: httpx.AsyncClient, api_key: str, voice_id: str, text: str, **options) -> bytes:
    """Collect one phrase into a single buffer, for offline listening."""
    return b"".join([chunk async for chunk in speech_bytes(http, api_key, voice_id, text, **options)])


def create_clone(api_key: str, name: str, sample_paths, *, transport=None,
                 timeout: float = CLONE_SECONDS) -> dict:
    """Enroll the owner's voice once and return the provider's JSON response.

    Only the encoded sample bytes leave this machine. The response carries
    ``voice_id`` to store as ``ELEVENLABS_VOICE_ID`` and ``requires_verification``,
    which means provider-side verification still has to be completed before
    that voice can speak.
    """
    if not api_key:
        raise ValueError("ELEVENLABS_API_KEY is required before creating a clone.")
    name = _checked_name(name)
    paths = _checked_samples(sample_paths)
    with ExitStack() as stack:
        # ``files`` matches the official client; let HTTPX build the boundary.
        files = [("files", (path.name, stack.enter_context(path.open("rb"))))
                 for path in paths]
        try:
            with _client(transport, timeout) as client:
                response = client.post(CLONE_URL, headers={"xi-api-key": api_key},
                                       data={"name": name}, files=files)
        except httpx.HTTPError as exc:
            raise TTSError(f"Voice enrollment failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise _failure("Voice enrollment", response.status_code, response.text)
    try:
        payload = response.json()
    except ValueError as exc:
        raise TTSError("Voice enrollment returned a malformed response") from exc
    if not isinstance(payload, dict) or not payload.get("voice_id"):
        raise TTSError("Voice enrollment returned no voice_id")
    return payload


def list_voices(api_key: str, *, transport=None, timeout: float = SPEECH_SECONDS) -> list[dict]:
    """List the account's voices: the cheapest live credential check available.

    This neither synthesizes audio nor spends character credits, so it is the
    first thing to run when a key, an account, or voice access is in doubt.
    """
    if not api_key:
        raise ValueError("ELEVENLABS_API_KEY is required before listing voices.")
    try:
        with _client(transport, timeout) as client:
            response = client.get(VOICES_URL, headers={"xi-api-key": api_key})
    except httpx.HTTPError as exc:
        raise TTSError(f"Voice list failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise _failure("Voice list", response.status_code, response.text)
    try:
        payload = response.json()
    except ValueError as exc:
        raise TTSError("Voice list returned a malformed response") from exc
    voices = payload.get("voices", []) if isinstance(payload, dict) else []
    if not isinstance(voices, list):
        raise TTSError("Voice list returned an unexpected shape")
    return [voice for voice in voices if isinstance(voice, dict)]
