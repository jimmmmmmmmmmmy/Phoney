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

import asyncio
import base64
import binascii
import json
import math
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .delivery import DEFAULT_DELIVERY, LATEST_REALTIME_MODEL
from .settings import ELEVENLABS_MODEL as ELEVENLABS_MODEL_PATTERN
from .settings import OUTPUT_FORMATS, TWILIO_FORMAT
from .settings import VOICE_ID as VOICE_ID_PATTERN

TTS_BASE = "https://api.elevenlabs.io/v1/text-to-speech"
CLONE_URL = "https://api.elevenlabs.io/v1/voices/add"
VOICES_URL = "https://api.elevenlabs.io/v1/voices"
DEFAULT_MODEL = LATEST_REALTIME_MODEL
DIALOGUE_URL = "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input"
# Bound downstream read-ahead without coalescing small provider chunks: waiting
# for a full 8,000-byte block would add a second of latency to telephone audio.
MAX_AUDIO_CHUNK_BYTES = 8000
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


def _checked_timeout(timeout) -> float:
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= 120):
        raise ValueError("Speech timeout must be between 0 and 120 seconds.")
    return float(timeout)


def _dialogue_model(model: str) -> bool:
    return model.startswith(("eleven_v3", "eleven_v4"))


def _audio_chunks(chunk: bytes):
    for start in range(0, len(chunk), MAX_AUDIO_CHUNK_BYTES):
        yield chunk[start:start + MAX_AUDIO_CHUNK_BYTES]


def _dialogue_message(raw, api_key: str) -> dict:
    """Validate the documented JSON protocol before passing any audio onward."""
    if not isinstance(raw, str):
        raise TTSError("Dialogue synthesis returned a non-JSON text frame")
    try:
        message = json.loads(raw)
    except ValueError as exc:
        raise TTSError("Dialogue synthesis returned malformed JSON") from exc
    if not isinstance(message, dict):
        raise TTSError("Dialogue synthesis returned an unexpected message shape")
    if message.get("error"):
        # Provider messages can echo rejected input. Never include our key in
        # errors surfaced by the relay, even if a malformed provider echoes it.
        detail = str(message.get("message") or message["error"]).replace(api_key, "[redacted]")
        detail = " ".join(detail.split())[:EXCERPT_CHARS]
        raise TTSError(f"Dialogue synthesis rejected the request: {detail}")
    for flag in ("is_final", "is_final_audio_for_turn"):
        if flag in message and type(message[flag]) is not bool:
            raise TTSError("Dialogue synthesis returned an invalid completion flag")
    if not any(key in message for key in ("audio", "is_final", "is_final_audio_for_turn")):
        raise TTSError("Dialogue synthesis returned an unknown message")
    return message


async def _dialogue_bytes(api_key: str, voice_id: str, text: str, *, model: str,
                          output_format: str, timeout: float, settings: dict,
                          sockets=None):
    """One documented closing dialogue WebSocket, not the legacy REST API.

    The current realtime guide explicitly supports ``eleven_v4_turbo``:
    https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/realtime-tdd
    ``flush`` does not document an utterance-completion boundary. Closing each
    phrase gives us ``is_final`` without changing playback ACK/barge-in logic.
    Cancellation closes the socket directly; it must not send a closing flush
    that would generate the cancelled buffered speech.
    """
    uri = DIALOGUE_URL + "?" + urlencode({"model_id": model, "output_format": output_format})
    websocket = None
    remaining = timeout

    async def network(operation):
        nonlocal remaining
        started = time.monotonic()
        try:
            return await asyncio.wait_for(operation, timeout=max(0, remaining))
        finally:
            remaining -= time.monotonic() - started

    try:
        websocket = await network(connect(uri, additional_headers={"xi-api-key": api_key},
                                          open_timeout=min(CONNECT_SECONDS, timeout), close_timeout=1,
                                          max_size=1024 * 1024, max_queue=4))
        if sockets is not None:
            sockets.add(websocket)
        initial = {"voices": [voice_id]}
        # The capability guide offers similarity for v4, but the current WS
        # schema only documents stability. Do not guess its other wire fields.
        if "stability" in settings:
            initial["voice_settings"] = {"stability": settings["stability"]}
        await network(websocket.send(json.dumps(initial)))
        await network(websocket.send(json.dumps({"inputs": [
            {"text": text, "voice_id": voice_id, "new_turn": False}
        ]})))
        # This flushes even short phrases below the server's buffer threshold
        # and ends with is_final, as the official guide does.
        await network(websocket.send(json.dumps({"close_socket": True})))
        received_audio = False
        while True:
            message = _dialogue_message(await network(websocket.recv()), api_key)
            if "audio" in message:
                encoded = message["audio"]
                if not isinstance(encoded, str):
                    raise TTSError("Dialogue synthesis returned invalid encoded audio")
                try:
                    chunk = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise TTSError("Dialogue synthesis returned invalid encoded audio") from exc
                if chunk:
                    received_audio = True
                    # A stream can be primed by a prefetch task and resumed by
                    # playback in another task. No task-owned timeout scope may
                    # span yield, and downstream playback consumes no budget.
                    for piece in _audio_chunks(chunk):
                        yield piece
            if message.get("is_final"):
                if not received_audio:
                    raise TTSError("Dialogue synthesis completed without audio")
                return
    except InvalidStatus as exc:
        raise TTSError("Dialogue connection was rejected", http_status=exc.response.status_code) from exc
    except TimeoutError as exc:
        raise TTSError("Dialogue synthesis timed out") from exc
    except (WebSocketException, OSError) as exc:
        # Cancellation/GeneratorExit deliberately pass through. Closed without
        # is_final is a failure, never a silently truncated successful phrase.
        raise TTSError(f"Dialogue synthesis failed: {type(exc).__name__}") from exc
    finally:
        if websocket is not None:
            try:
                await websocket.close()
            finally:
                if sockets is not None:
                    sockets.discard(websocket)


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
                       timeout: float = SPEECH_SECONDS, delivery=None, previous_text=None,
                       _sockets=None):
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
    timeout = _checked_timeout(timeout)
    delivery = DEFAULT_DELIVERY if delivery is None else delivery
    text = _checked_text(delivery.text_for(text, model))
    settings = delivery.settings_for(model)
    if previous_text is not None and (not isinstance(previous_text, str)
                                      or len(previous_text) > MAX_TEXT_CHARS):
        raise ValueError(f"Previous speech text is limited to {MAX_TEXT_CHARS} characters.")
    if _dialogue_model(model):
        stream = _dialogue_bytes(api_key, voice_id, text, model=model,
                                 output_format=output_format, timeout=timeout,
                                 settings=settings, sockets=_sockets)
        try:
            async for chunk in stream:
                yield chunk
        finally:
            await stream.aclose()
        return
    payload = {"text": text, "model_id": model}
    if settings:
        payload["voice_settings"] = settings
    if previous_text:
        payload["previous_text"] = previous_text
    try:
        async with http.stream(
            "POST",
            f"{TTS_BASE}/{voice_id}/stream",
            params={"output_format": output_format},
            headers={"xi-api-key": api_key, "content-type": "application/json"},
            json=payload,
            timeout=httpx.Timeout(timeout, connect=CONNECT_SECONDS),
        ) as response:
            if response.status_code != 200:
                raise _failure("Speech request", response.status_code,
                               (await response.aread()).decode("utf-8", "replace"))
            received_audio = False
            async for chunk in response.aiter_bytes():
                if chunk:
                    received_audio = True
                    for piece in _audio_chunks(chunk):
                        yield piece
            if not received_audio:
                raise TTSError("Speech request completed without audio")
    except httpx.HTTPError as exc:
        # Cancellation is a BaseException and deliberately passes through.
        raise TTSError(f"Speech request failed: {type(exc).__name__}") from exc


class SpeechSession:
    """Reply-scoped speech transport with bounded chunks and legacy continuity.

    Each dialogue phrase owns a closing socket: no idle dialogue capacity is
    held between phrases. Legacy REST phrases inherit the last fully generated
    text. A prefetched phrase may explicitly receive the currently playing
    phrase as ``previous_text``; cancelling the reply discards this session.
    Reader tasks must be cancelled/gathered before leaving the context, so
    cleanup can close both suspended HTTP iterators and dialogue sockets.
    """

    def __init__(self, http: httpx.AsyncClient, api_key: str, voice_id: str, *,
                 model: str = DEFAULT_MODEL, output_format: str = TWILIO_FORMAT,
                 timeout: float = SPEECH_SECONDS, delivery=None):
        self.http, self._api_key = http, api_key
        self.voice_id = _checked_voice_id(voice_id)
        self.model = _checked_model(model)
        self.output_format = _checked_format(output_format)
        self.timeout = _checked_timeout(timeout)
        self.delivery = DEFAULT_DELIVERY if delivery is None else delivery
        self._previous_text = ""
        self._issued = 0
        self._completed = -1
        self._streams = set()
        self._sockets = set()
        self._closed = False

    async def __aenter__(self):
        if self._closed:
            raise TTSError("Speech session is closed")
        return self

    async def __aexit__(self, *_exc):
        await self.close()

    async def speech_bytes(self, text: str, *, previous_text=None):
        if self._closed:
            raise TTSError("Speech session is closed")
        text = _checked_text(text)
        issued = self._issued
        self._issued += 1
        previous = self._previous_text if previous_text is None else previous_text
        stream = speech_bytes(self.http, self._api_key, self.voice_id, text,
                              model=self.model, output_format=self.output_format,
                              timeout=self.timeout, delivery=self.delivery,
                              previous_text=previous, _sockets=self._sockets)
        self._streams.add(stream)
        try:
            async for chunk in stream:
                yield chunk
            if issued > self._completed:
                self._completed = issued
                self._previous_text = text
        finally:
            try:
                await stream.aclose()
            finally:
                self._streams.discard(stream)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        # Closing sockets directly never flushes speech cancelled by barge-in.
        if self._sockets:
            await asyncio.gather(*(socket.close() for socket in tuple(self._sockets)),
                                 return_exceptions=True)
        for stream in tuple(self._streams):
            await stream.aclose()


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
