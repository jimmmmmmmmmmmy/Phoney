"""Bounded native Modulate streaming adapter for completed-capture replay.

This module is deliberately isolated from the web application.  It only sends
audio when its caller explicitly invokes :func:`stream_inbound_pcm`.
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
import io
import json
import math
from pathlib import PurePath
import time
from typing import Any, AsyncIterable, Callable, Literal, Protocol
from urllib.parse import urlencode
import wave

import httpx

from integrations.contracts import AudioFrame


STREAM_URL = "wss://platform.modulate.ai/api/velma-2-synthetic-voice-detection-streaming"
BATCH_URL = "https://platform.modulate.ai/api/velma-2-synthetic-voice-detection-batch"
MAX_AUDIO_SECONDS = 120  # Conservative local experiment limit; not a provider limit.
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_BATCH_AUDIO_BYTES = 100 * 1024 * 1024
MAX_OBSERVATIONS = 512
DEFAULT_DEADLINE_SECONDS = 45.0
Verdict = Literal["synthetic", "non-synthetic", "unknown"]


class ModulateError(RuntimeError):
    """A sanitized batch-provider or response-contract failure."""


class _Socket(Protocol):
    async def send(self, message: str | bytes) -> None: ...
    def __aiter__(self) -> Any: ...


Connector = Callable[[str], AbstractAsyncContextManager[_Socket]]


def _batch_frame(frame: object, source_start_ms: int, duration_ms: int) -> dict[str, object]:
    if not isinstance(frame, dict):
        raise ModulateError("invalid_provider_frame")
    start = frame.get("start_time_ms")
    end = frame.get("end_time_ms")
    verdict = frame.get("verdict")
    confidence = frame.get("confidence")
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= duration_ms
            or not isinstance(verdict, str) or verdict not in {"synthetic", "non-synthetic", "no-content"}
            or type(confidence) not in {int, float} or not math.isfinite(confidence)
            or not 0 <= float(confidence) <= 1):
        raise ModulateError("invalid_provider_frame")
    return {"start_ms": source_start_ms + start, "end_ms": source_start_ms + end,
            "verdict": verdict, "confidence": float(confidence),
            "synthetic_probability": None}


def detect_audio(
    audio_bytes: bytes,
    *,
    filename: str,
    api_key: str,
    session_id: str,
    track: Literal["inbound", "outbound"],
    source_start_ms: int = 0,
    client: httpx.Client | None = None,
) -> dict[str, object]:
    """Upload one explicit WAV or MP3 file to Modulate's synchronous batch API."""
    if not api_key or not session_id or track not in {"inbound", "outbound"}:
        raise ValueError("api_key, session_id, and a valid capture track are required")
    if type(source_start_ms) is not int or source_start_ms < 0:
        raise ValueError("source_start_ms must be a nonnegative integer")
    if (not isinstance(filename, str) or not filename or PurePath(filename).name != filename
            or not isinstance(audio_bytes, bytes) or not 0 < len(audio_bytes) <= MAX_BATCH_AUDIO_BYTES):
        raise ValueError("expected one bounded local audio file")

    suffix = PurePath(filename).suffix.lower()
    media_types = {".wav": "audio/wav", ".mp3": "audio/mpeg"}
    if suffix not in media_types:
        raise ValueError("only .wav and .mp3 batch files are supported")

    wav_samples: int | None = None
    if suffix == ".wav":
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(),
                        audio.getcomptype()) != (1, 2, 8000, "NONE"):
                    raise ValueError("expected mono 8000 Hz PCM16 WAV")
                wav_samples = audio.getnframes()
                if not 4 * 8000 <= wav_samples <= 60 * 8000:
                    raise ValueError("WAV clips must be from 4 through 60 seconds")
                if len(audio.readframes(wav_samples)) != wav_samples * 2:
                    raise ValueError("truncated PCM WAV clip")
        except (wave.Error, EOFError) as exc:
            raise ValueError("invalid PCM WAV clip") from exc

    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(75.0, connect=10.0),
                                  follow_redirects=False)
    response_body = bytearray()
    try:
        with http.stream("POST", BATCH_URL, headers={"X-API-Key": api_key},
                         files={"upload_file": (filename, audio_bytes, media_types[suffix])},
                         timeout=httpx.Timeout(75.0, connect=10.0),
                         follow_redirects=False) as response:
            if response.status_code != 200:
                raise ModulateError(f"provider_http_{response.status_code}")
            for chunk in response.iter_bytes(chunk_size=16 * 1024):
                if len(response_body) + len(chunk) > MAX_MESSAGE_BYTES:
                    raise ModulateError("provider_response_too_large")
                response_body.extend(chunk)
    except httpx.HTTPError as exc:
        raise ModulateError("provider_transport_failed") from exc
    finally:
        if owns_client:
            http.close()

    try:
        body = json.loads(response_body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ModulateError("invalid_provider_json") from exc
    if not isinstance(body, dict):
        raise ModulateError("invalid_provider_result")
    duration_ms = body.get("duration_ms")
    if type(duration_ms) is not int or duration_ms <= 0:
        raise ModulateError("invalid_provider_duration")
    if wav_samples is not None and abs(duration_ms - wav_samples / 8) > 1:
        raise ModulateError("provider_duration_mismatch")
    provider_filename = body.get("filename")
    if provider_filename is not None and not isinstance(provider_filename, str):
        raise ModulateError("invalid_provider_filename")
    raw_frames = body.get("frames")
    if not isinstance(raw_frames, list) or len(raw_frames) > MAX_OBSERVATIONS:
        raise ModulateError("invalid_provider_frames")
    frames = [_batch_frame(frame, source_start_ms, duration_ms) for frame in raw_frames]
    if any(left["start_ms"] > right["start_ms"] for left, right in zip(frames, frames[1:])):
        raise ModulateError("out_of_order_provider_frame")
    usable = any(frame["verdict"] != "no-content" for frame in frames)
    return {"provider": "modulate", "score_kind": "verdict_confidence",
            "model_version": None, "session_id": session_id, "track": track,
            "source_start_ms": source_start_ms, "duration_ms": duration_ms,
            "status": "ok" if usable else "insufficient_audio", "frames": frames}


@dataclass(frozen=True, slots=True)
class DetectionObservation:
    """One provider time interval, retaining Modulate's uncalibrated score."""

    session_id: str
    stream_id: str
    track: Literal["inbound"]
    start_ms: int
    end_ms: int
    verdict: Verdict
    provider_verdict: str
    confidence: float
    confidence_kind: str = "verdict_confidence"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DetectionReport:
    """A completed offline attempt. Errors and missing content are unknown."""

    session_id: str | None
    stream_id: str | None
    track: Literal["inbound"] = "inbound"
    status: Verdict = "unknown"
    observations: tuple[DetectionObservation, ...] = ()
    reason: str | None = None
    submitted_audio_ms: int = 0
    elapsed_ms: int = 0
    coverage_limited: bool = False
    source_start_ms: int = 0

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["observations"] = [item.to_dict() for item in self.observations]
        return result


def _default_connector(url: str) -> AbstractAsyncContextManager[_Socket]:
    # Import lazily so dry-run and repository tests do not initialize a provider client.
    import websockets

    return websockets.connect(url, open_timeout=10, close_timeout=5, max_size=MAX_MESSAGE_BYTES)


def _unknown(session_id: str | None, stream_id: str | None, reason: str, *,
             submitted_audio_ms: int = 0, elapsed_ms: int = 0) -> DetectionReport:
    return DetectionReport(session_id=session_id, stream_id=stream_id, reason=reason,
                           submitted_audio_ms=submitted_audio_ms, elapsed_ms=elapsed_ms)


def _parse_observation(message: object, session_id: str, stream_id: str,
                       submitted_audio_ms: int) -> DetectionObservation:
    if not isinstance(message, dict):
        raise ValueError("invalid_provider_message")
    frame = message.get("frame")
    if not isinstance(frame, dict):
        raise ValueError("invalid_provider_frame")
    start, end = frame.get("start_time_ms"), frame.get("end_time_ms")
    provider_verdict, confidence = frame.get("verdict"), frame.get("confidence")
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= submitted_audio_ms
            or not isinstance(provider_verdict, str) or provider_verdict not in {"synthetic", "non-synthetic", "no-content"}
            or type(confidence) not in {int, float} or not math.isfinite(confidence)
            or not 0 <= float(confidence) <= 1):
        raise ValueError("invalid_provider_frame")
    verdict: Verdict = provider_verdict if provider_verdict != "no-content" else "unknown"
    return DetectionObservation(session_id, stream_id, "inbound", start, end, verdict,
                                provider_verdict, float(confidence))


class _DetectionFailure(Exception):
    """Internal reason code; never contains provider-controlled text."""


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, _DetectionFailure):
        return str(exc)
    for child in getattr(exc, "exceptions", ()):
        reason = _failure_reason(child)
        if reason != "provider_transport_failed":
            return reason
    return "provider_transport_failed"


async def stream_inbound_pcm(
    frames: AsyncIterable[AudioFrame],
    *,
    api_key: str,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    max_audio_seconds: int = MAX_AUDIO_SECONDS,
    collection_seconds: float | None = None,
    on_input_complete: Callable[[], None] | None = None,
    on_observation: Callable[[DetectionObservation], None] | None = None,
    connector: Connector | None = None,
) -> DetectionReport:
    """Collect bounded inbound audio, then allow a separate finalization deadline.

    Provider intervals are relative to submitted audio and are translated onto
    the first inbound frame's source timeline. Missing or overlapping samples
    invalidate the attempt. The audio cap ends input normally, retaining useful
    evidence with an explicit partial-coverage marker.
    """
    started = time.monotonic()
    if not isinstance(api_key, str) or not api_key:
        return _unknown(None, None, "missing_api_key")
    if (type(max_audio_seconds) is not int or not 1 <= max_audio_seconds <= MAX_AUDIO_SECONDS
            or not isinstance(deadline_seconds, (int, float)) or isinstance(deadline_seconds, bool)
            or not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= 120):
        return _unknown(None, None, "invalid_local_limits")
    collection_seconds = deadline_seconds if collection_seconds is None else collection_seconds
    if (not isinstance(collection_seconds, (int, float)) or isinstance(collection_seconds, bool)
            or not math.isfinite(collection_seconds) or not 0 < collection_seconds <= 150):
        return _unknown(None, None, "invalid_local_limits")

    session_id: str | None = None
    stream_id: str | None = None
    submitted_bytes = 0
    source_start_ms = 0
    expected_sample: int | None = None
    saw_non_silent_audio = False
    observations: list[DetectionObservation] = []
    done = False
    sender_finished = False
    coverage_limited = False
    reason: str | None = None
    max_bytes = max_audio_seconds * 8000 * 2
    input_ended = asyncio.Event()
    results_ended = asyncio.Event()

    # Query authentication is required by this endpoint; never log this URL.
    query = urlencode({"api_key": api_key, "audio_format": "s16le", "sample_rate": 8000,
                       "num_channels": 1})
    url = f"{STREAM_URL}?{query}"
    try:
        async with asyncio.timeout(float(collection_seconds + deadline_seconds + 10)):
            async with (connector or _default_connector)(url) as socket:
                async def send_audio() -> None:
                    nonlocal session_id, stream_id, submitted_bytes, saw_non_silent_audio
                    nonlocal sender_finished, coverage_limited, source_start_ms, expected_sample
                    try:
                        async with asyncio.timeout(float(collection_seconds)):
                            async for frame in frames:
                                if frame.track != "inbound":
                                    continue
                                if not frame.pcm_s16le:
                                    continue
                                if session_id is None:
                                    session_id, stream_id = frame.session_id, frame.stream_id
                                    source_start_ms = frame.timestamp_ms
                                    expected_sample = frame.timestamp_ms * 8
                                elif (frame.session_id, frame.stream_id) != (session_id, stream_id):
                                    raise _DetectionFailure("mixed_sessions")
                                if frame.timestamp_ms * 8 != expected_sample:
                                    raise _DetectionFailure("audio_discontinuity")
                                expected_sample += frame.sample_count
                                payload = frame.pcm_s16le[:max_bytes - submitted_bytes]
                                submitted_bytes += len(payload)
                                saw_non_silent_audio = saw_non_silent_audio or any(payload)
                                try:
                                    async with asyncio.timeout(float(deadline_seconds)):
                                        await socket.send(payload)
                                except TimeoutError as exc:
                                    raise _DetectionFailure("provider_timeout") from exc
                                if submitted_bytes == max_bytes:
                                    coverage_limited = True
                                    break
                    except TimeoutError as exc:
                        raise _DetectionFailure("audio_collection_timeout") from exc
                    sender_finished = True
                    if on_input_complete is not None:
                        on_input_complete()
                    # Include a stalled EOF send in the finalization deadline.
                    input_ended.set()
                    try:
                        async with asyncio.timeout(float(deadline_seconds)):
                            await socket.send("")
                    except TimeoutError as exc:
                        raise _DetectionFailure("provider_timeout") from exc

                async def receive_results() -> None:
                    nonlocal done
                    async for raw in socket:
                        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
                            raise _DetectionFailure("invalid_provider_response")
                        try:
                            message = json.loads(raw)
                        except (TypeError, ValueError) as exc:
                            raise _DetectionFailure("invalid_provider_response") from exc
                        if not isinstance(message, dict):
                            raise _DetectionFailure("invalid_provider_response")
                        kind = message.get("type")
                        if kind == "frame":
                            if session_id is None:
                                raise _DetectionFailure("invalid_provider_response")
                            try:
                                observation = _parse_observation(message, session_id, stream_id or "",
                                                                 max_bytes // 16)
                            except ValueError as exc:
                                raise _DetectionFailure("invalid_provider_response") from exc
                            if (observations and observation.start_ms < observations[-1].start_ms
                                    or observation.end_ms > submitted_bytes // 16
                                    or len(observations) >= MAX_OBSERVATIONS):
                                raise _DetectionFailure("invalid_provider_response")
                            observations.append(observation)
                            if on_observation is not None and (saw_non_silent_audio
                                    or observation.provider_verdict == "no-content"):
                                translated = DetectionObservation(
                                    observation.session_id, observation.stream_id, observation.track,
                                    observation.start_ms + source_start_ms, observation.end_ms + source_start_ms,
                                    observation.verdict, observation.provider_verdict, observation.confidence)
                                try:
                                    on_observation(translated)
                                except Exception:
                                    pass  # An observer must never stop provider or telephony processing.
                        elif kind == "done":
                            count, duration = message.get("frame_count"), message.get("duration_ms")
                            if (not sender_finished or type(count) is not int or count != len(observations)
                                    or type(duration) is not int or duration <= 0
                                    or abs(duration - submitted_bytes / 16) > 1
                                    or any(item.end_ms > duration for item in observations)):
                                raise _DetectionFailure("invalid_provider_response")
                            done = True
                            results_ended.set()
                            return
                        elif kind == "error":
                            raise _DetectionFailure("provider_reported_error")
                        else:
                            raise _DetectionFailure("invalid_provider_response")
                    raise _DetectionFailure("provider_incomplete")

                async def finalize_deadline() -> None:
                    await input_ended.wait()
                    try:
                        async with asyncio.timeout(float(deadline_seconds)):
                            await results_ended.wait()
                    except TimeoutError as exc:
                        raise _DetectionFailure("provider_timeout") from exc

                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(send_audio())
                    tasks.create_task(receive_results())
                    tasks.create_task(finalize_deadline())
    except TimeoutError:
        reason = "provider_timeout"
    except Exception as exc:
        reason = _failure_reason(exc)

    elapsed_ms = round((time.monotonic() - started) * 1000)
    submitted_audio_ms = submitted_bytes // 16
    translated = tuple(DetectionObservation(
        item.session_id, item.stream_id, item.track,
        item.start_ms + source_start_ms, item.end_ms + source_start_ms,
        item.verdict, item.provider_verdict, item.confidence,
    ) for item in observations)
    if reason or not done:
        retained = translated if reason in {"provider_timeout", "provider_transport_failed",
            "provider_reported_error", "provider_incomplete", "audio_collection_timeout"} else ()
        return DetectionReport(session_id=session_id, stream_id=stream_id,
                               reason=reason or "provider_incomplete", observations=retained,
                               submitted_audio_ms=submitted_audio_ms, elapsed_ms=elapsed_ms,
                               source_start_ms=source_start_ms, coverage_limited=coverage_limited)
    usable = saw_non_silent_audio and any(item.verdict != "unknown" for item in translated)
    verdicts = {item.verdict for item in translated if item.verdict != "unknown"}
    status: Verdict = next(iter(verdicts)) if usable and len(verdicts) == 1 else "unknown"
    return DetectionReport(session_id=session_id, stream_id=stream_id, status=status,
                           observations=translated,
                           reason=None if usable else "no_usable_content",
                           submitted_audio_ms=submitted_audio_ms, elapsed_ms=elapsed_ms,
                           source_start_ms=source_start_ms, coverage_limited=coverage_limited)
