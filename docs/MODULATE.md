# Modulate integration recipe

Use the **documented Models API**, beginning with one completed Build 2 capture and a 4–60 second clip. Copy the adapter below into a partner-owned module when detection implementation begins. This document is a recipe: no Modulate credentials, audio uploads, detector, or live call hooks were enabled while writing it.

**Contract checked: 2026-09-26.** The machine-readable [batch OpenAPI](https://docs.modulate.ai/api/velma_2_synthetic_voice_detection_batch.yaml) and [streaming AsyncAPI](https://docs.modulate.ai/api/velma_2_synthetic_voice_detection_streaming.yaml) are linked from the official [documentation index](https://docs.modulate.ai/llms.txt). They define the same request paths as the developer reference. Their `info.version` value is `0.0.0`; that is not a version identifier for the deployed model.

For the full evaluation and application design, use [DEEPFAKE_DETECTION.md](DEEPFAKE_DETECTION.md). For the existing local audio interface, use [PARTNER_HANDOFF.md](PARTNER_HANDOFF.md).

## Resolve the conflicting examples before coding

The [deepfake landing page](https://www.modulate.ai/lp/deepfake-detection-api) shows an unsuffixed path, Bearer authentication, and an `audio` field. The [general API overview](https://www.modulate.ai/api-overview) shows `api.modulate.ai/v1/detect`, Bearer authentication, and a `file` field. Those examples disagree with the dedicated developer references and schemas. This project chooses the latter as its implementation contract; it does not guess that the marketing examples are equivalent aliases.

| Operation | Verified request |
| --- | --- |
| Batch | `POST https://platform.modulate.ai/api/velma-2-synthetic-voice-detection-batch` |
| Batch authentication | Header `X-API-Key` |
| Batch body | Multipart file part `upload_file`; no model-selection JSON |
| Streaming | `wss://platform.modulate.ai/api/velma-2-synthetic-voice-detection-streaming` |
| Streaming authentication | Query parameter `api_key` |

Sources: [batch reference](https://docs.modulate.ai/api-reference/svd/batch), [streaming reference](https://docs.modulate.ai/api-reference/svd/streaming), and [authentication guide](https://docs.modulate.ai/guides/authentication).

## Batch input and result

Upload a complete WAV container. The documented file ceiling is 100 MB; common accepted alternatives include FLAC, MP3, and Ogg. Batch returns JSON in the same HTTP response, with `filename`, `duration_ms`, and a `frames` array. Each frame has `start_time_ms`, `end_time_ms`, `verdict`, and `confidence`. This is a synchronous upload, not a job-ID/polling workflow. [Batch reference](https://docs.modulate.ai/api-reference/svd/batch).

The capability guide specifies a 0.5-second minimum, recommends 4–60 seconds, and describes four-second batch windows with shorter audio padded internally. It also describes silence trimming while retaining offsets relative to the submitted file. Confidence belongs to the returned verdict: **do not convert a natural-speech confidence into `1 - confidence` and label it a synthetic probability**. A `no-content` verdict represents no usable content, not verified natural speech. [Deepfake capability guide](https://docs.modulate.ai/get-started/deepfake).

The adapter below deliberately enforces a smaller local contract: a 4–60 second, mono, 8000 Hz PCM16 WAV from this repository. This reduces accidental uploads of an entire call and makes fixtures predictable. The four-second lower bound is our integration policy, not the provider's minimum. There is no documented batch `sample_rate` form parameter; the WAV header carries that information. The batch reference does not list a separate accepted sample-rate range. Use the native 8000 Hz WAV for the first provider acceptance check; streaming explicitly lists 8000 Hz support.

Build 2's `inbound.wav` is caller input. `outbound.wav` is caller playback, which can contain conference output or hold audio. Select one track and keep its name in every result. Never concatenate the two tracks as if they were one speaker. The provider describes this model as intended for single-speaker audio. [Model pricing and descriptions](https://platform.modulate.ai/pricing).

## Runnable HTTPX adapter

Create `partner_detection/__init__.py` and save this block as `partner_detection/modulate.py` when implementing the partner component. The existing development requirements include HTTPX. The module performs no request on import; `detect_wav` makes one request when explicitly called. It does not retry or alter a phone call.

```python
from __future__ import annotations

import io
import json
import math
import wave
from typing import Any

import httpx


BATCH_URL = "https://platform.modulate.ai/api/velma-2-synthetic-voice-detection-batch"
VERDICTS = {"synthetic", "non-synthetic", "no-content"}
MAX_RESPONSE_BYTES = 1024 * 1024


class ModulateError(RuntimeError):
    pass


def integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ModulateError(f"Invalid provider field: {label}")
    return value


def checked_duration(value: Any, submitted_samples: int) -> int:
    duration = integer(value, "duration_ms")
    # Local tolerance for integer-ms rounding, not a provider guarantee.
    if duration == 0 or abs(duration - submitted_samples / 8) > 1:
        raise ModulateError("Provider duration differs from the submitted PCM")
    return duration


def normalize_frame(frame: dict, source_start_ms: int, *, duration_ms: int) -> dict:
    if not isinstance(frame, dict):
        raise ModulateError("Invalid provider frame")
    start = integer(frame.get("start_time_ms"), "start_time_ms")
    end = integer(frame.get("end_time_ms"), "end_time_ms")
    verdict, confidence = frame.get("verdict"), frame.get("confidence")
    if (not 0 <= start < end <= duration_ms
            or not isinstance(verdict, str) or verdict not in VERDICTS
            or type(confidence) not in (int, float)
            or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise ModulateError("Invalid provider verdict, interval, or confidence")
    return {"start_ms": source_start_ms + start,
            "end_ms": source_start_ms + end,
            "verdict": verdict, "confidence": float(confidence),
            "synthetic_probability": None}


class ModulateDetector:
    def __init__(self, api_key: str, client: httpx.Client | None = None):
        if not api_key:
            raise ValueError("MODULATE_API_KEY is required")
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(
            timeout=httpx.Timeout(75.0, connect=10.0),
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def detect_wav(self, wav_bytes: bytes, *, session_id: str, track: str,
                   source_start_ms: int = 0) -> dict:
        if track not in {"inbound", "outbound"} or not session_id:
            raise ValueError("Supply a session ID and original capture track")
        if type(source_start_ms) is not int or source_start_ms < 0:
            raise ValueError("source_start_ms must be a nonnegative integer")
        if not isinstance(wav_bytes, bytes) or len(wav_bytes) > 2_000_000:
            raise ValueError("Expected a small PCM WAV clip")
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as audio:
                if (audio.getnchannels(), audio.getsampwidth(),
                    audio.getframerate(), audio.getcomptype()) != (1, 2, 8000, "NONE"):
                    raise ValueError("Expected mono 8000 Hz PCM16 WAV")
                samples = audio.getnframes()
                if not 4 * 8000 <= samples <= 60 * 8000:
                    raise ValueError("This adapter accepts clips from 4 through 60 seconds")
                if len(audio.readframes(samples)) != samples * 2:
                    raise ValueError("Truncated PCM WAV clip")
        except (wave.Error, EOFError) as exc:
            raise ValueError("Invalid PCM WAV clip") from exc

        raw_body = bytearray()
        try:
            with self._client.stream(
                "POST", BATCH_URL,
                headers={"X-API-Key": self._api_key},
                files={"upload_file": ("clip.wav", wav_bytes, "audio/wav")},
                timeout=httpx.Timeout(75.0, connect=10.0), follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    # Keep raw provider bodies and headers out of default logs.
                    raise ModulateError(f"Provider HTTP {response.status_code}; result is unknown")
                for chunk in response.iter_bytes(chunk_size=16 * 1024):
                    if len(raw_body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ModulateError("Provider response exceeds the 1 MiB limit")
                    raw_body.extend(chunk)
        except httpx.HTTPError as exc:
            raise ModulateError("Provider transport failed; result is unknown") from exc
        try:
            body = json.loads(raw_body)
        except ValueError as exc:
            raise ModulateError("Provider returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ModulateError("Provider returned an invalid result object")
        duration_ms = checked_duration(body.get("duration_ms"), samples)
        if "filename" not in body or (body["filename"] is not None
                                       and not isinstance(body["filename"], str)):
            raise ModulateError("Provider returned an invalid filename field")
        raw_frames = body.get("frames")
        if not isinstance(raw_frames, list) or len(raw_frames) > 10_000:
            raise ModulateError("Provider returned an invalid frame collection")
        bound_ms = min(duration_ms, math.ceil(samples / 8))
        frames = [normalize_frame(frame, source_start_ms, duration_ms=bound_ms)
                  for frame in raw_frames]
        if any(left["start_ms"] > right["start_ms"]
               for left, right in zip(frames, frames[1:])):
            raise ModulateError("Provider returned frames out of order")
        usable = any(frame["verdict"] != "no-content" for frame in frames)
        return {"provider": "modulate", "score_kind": "verdict_confidence",
                "model_version": None, "session_id": session_id, "track": track,
                "source_start_ms": source_start_ms, "duration_ms": duration_ms,
                "status": "ok" if usable else "insufficient_audio", "frames": frames}
```

The URL, request fields, and response fields follow the [batch OpenAPI schema](https://docs.modulate.ai/api/velma_2_synthetic_voice_detection_batch.yaml). Validation, resource ownership, clip limits, timeout values, and normalized fields are project design choices. Let HTTPX create the multipart `Content-Type` boundary; do not manually set that header. A caller that supplies its own HTTPX client owns that client's lifetime. The adapter caps the decoded response at 1 MiB before JSON parsing. It requires the provider's duration to match the submitted sample count within **one millisecond**, a local tolerance for integer rounding, and bounds every frame to both that duration and the actual submitted clip. A zero duration, zero-length frame, or interval outside those bounds fails validation.

This synchronous adapter is for an explicit offline experiment. HTTPX's 75-second timeout applies to individual I/O operations, **not a total wall-clock deadline**; a slow response can take longer. A later live worker must use cancellable asynchronous I/O with an enclosing whole-operation deadline, a bounded queue, and a separate outcome for timeout. Do not run this synchronous request on FastAPI's Twilio event loop.

`model_version: null` means the endpoint did not report an immutable deployed-model version. `synthetic_probability: null` is intentional. Preserve the provider verdict and its confidence; calibrating a separate project score is later evaluation work. Adding `source_start_ms` maps a cropped clip back to the original capture timeline. Because leading/trailing silence can be removed by the provider, use its returned offsets rather than deriving timestamps from frame index.

## Run one explicit completed-capture experiment

The following example belongs in a local partner runner after the module above exists. It reads a ten-second caller-input interval beginning five seconds into a completed recording, wraps that PCM in a new WAV, and uploads only that clip. It requires the chosen interval to exist in full. It also rejects a selected track with any padded gaps, or a capture with dropped/rejected messages. Because the current manifest has aggregate gap counts rather than gap intervals, this deliberately rejects gaps anywhere in that track, even outside the crop. Change `manifest`, `start_ms`, and `duration_ms` deliberately for your fixture.

```python
import io
import json
import os
import wave

from integrations.replay import read_completed_capture
from partner_detection.modulate import ModulateDetector

manifest = "/path/to/completed/capture/manifest.json"
track_name, start_ms, duration_ms = "inbound", 5000, 10000
capture = read_completed_capture(manifest)
quality = json.loads(capture.manifest_path.read_text())
quality_counts = [quality["tracks"][track_name].get("gap_samples"),
                  quality.get("counters", {}).get("dropped_messages"),
                  quality.get("counters", {}).get("rejected_messages")]
if any(type(count) is not int or count != 0 for count in quality_counts):
    raise ValueError("Use a capture with no padded gaps or lost/rejected messages")
track = next(item for item in capture.tracks if item.name == track_name)
start_sample, sample_count = start_ms * 8, duration_ms * 8
if start_sample + sample_count > track.samples:
    raise ValueError("Choose an interval fully present in this track")
with wave.open(str(track.path), "rb") as audio:
    audio.setpos(start_sample)
    pcm = audio.readframes(sample_count)
if len(pcm) != sample_count * 2:
    raise ValueError("Capture changed or was truncated")
container = io.BytesIO()
with wave.open(container, "wb") as audio:
    audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
    audio.writeframes(pcm)
with ModulateDetector(os.environ["MODULATE_API_KEY"]) as detector:
    result = detector.detect_wav(container.getvalue(), session_id=capture.session_id,
                                 track=track_name, source_start_ms=start_ms)
print(json.dumps(result, indent=2))
```

Use a test clip you intend to send to the provider. The example reads the API key from the partner process environment; it does not print the key, put it in a command argument, or modify the server's `.env`. Capture files and detector results remain outside committed fixtures unless the team explicitly chooses shareable examples.

## Optional next stage: native WebSocket streaming

The verified streaming interface accepts binary audio and emits JSON results. Send an empty **text** message to finish, then wait for `done`; closing immediately can lose buffered results. For Build 2 decoded frames, use `audio_format=s16le&sample_rate=8000&num_channels=1`. If a later adapter forwards Twilio's original decoded-base64 wire payload instead, use `audio_format=mulaw&sample_rate=8000&num_channels=1`. Never label PCM bytes as μ-law, and never send a WAV header with a raw format. [Streaming reference](https://docs.modulate.ai/api-reference/svd/streaming) and [format documentation](https://docs.modulate.ai/get-started/deepfake).

A future streaming partner can use the same `normalize_frame` helper from the batch module. This runnable function is an isolated clip experiment using the `websockets` package; it is not registered in the live Twilio server:

```python
import asyncio
import json
import math
from urllib.parse import urlencode

import websockets

from partner_detection.modulate import ModulateError, checked_duration, integer, normalize_frame


async def stream_pcm_clip(pcm_s16le: bytes, api_key: str,
                          source_start_ms: int = 0) -> list[dict]:
    if (not api_key or not isinstance(pcm_s16le, bytes)
            or not 4 * 16000 <= len(pcm_s16le) <= 60 * 16000):
        raise ValueError("Supply a key and 4–60 seconds of mono 8000 Hz PCM16")
    if len(pcm_s16le) % 2 or type(source_start_ms) is not int or source_start_ms < 0:
        raise ValueError("Supply complete PCM16 samples and a nonnegative offset")
    query = urlencode({"api_key": api_key, "audio_format": "s16le",
                       "sample_rate": 8000, "num_channels": 1})
    url = "wss://platform.modulate.ai/api/velma-2-synthetic-voice-detection-streaming?" + query
    frames, saw_done = [], False
    samples, sent_bytes, end_started = len(pcm_s16le) // 2, 0, False
    bound_ms = math.ceil(samples / 8)
    async with asyncio.timeout(90):
        async with websockets.connect(url, open_timeout=10, close_timeout=5,
                                      max_size=1024 * 1024) as socket:
            async def send():
                nonlocal sent_bytes, end_started
                for offset in range(0, len(pcm_s16le), 1600):
                    chunk = pcm_s16le[offset:offset + 1600]
                    await socket.send(chunk)
                    sent_bytes += len(chunk)
                end_started = True
                await socket.send("")

            async def receive():
                nonlocal saw_done
                async for raw in socket:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ModulateError("Invalid streaming message")
                    if message.get("type") == "frame":
                        frame = normalize_frame(message.get("frame"), source_start_ms,
                                                duration_ms=bound_ms)
                        if frames and frame["start_ms"] < frames[-1]["start_ms"]:
                            raise ModulateError("Streaming frames arrived out of order")
                        frames.append(frame)
                        if len(frames) > 10_000:
                            raise ModulateError("Streaming result exceeded local bound")
                    elif message.get("type") == "done":
                        if sent_bytes != len(pcm_s16le) or not end_started:
                            raise ModulateError("Provider ended before all input was sent")
                        duration_ms = checked_duration(message.get("duration_ms"), samples)
                        count = integer(message.get("frame_count"), "frame_count")
                        if count != len(frames):
                            raise ModulateError("Streaming summary does not match received frames")
                        if any(frame["end_ms"] - source_start_ms > duration_ms for frame in frames):
                            raise ModulateError("Streaming frame exceeds the final duration")
                        saw_done = True
                        return
                    elif message.get("type") == "error":
                        raise ModulateError("Streaming provider rejected or failed analysis")
                    else:
                        raise ModulateError("Unknown streaming message type")
                raise ModulateError("Streaming connection ended before done")

            async with asyncio.TaskGroup() as group:
                group.create_task(send())
                group.create_task(receive())
    if not saw_done:
        raise ModulateError("Streaming result is incomplete")
    return frames
```

Binary input, the end-of-stream text sentinel, and `frame`/`done`/`error` message types are defined in the [streaming AsyncAPI schema](https://docs.modulate.ai/api/velma_2_synthetic_voice_detection_streaming.yaml). This example sends a completed clip as quickly as the socket accepts it; it is not a measurement of live latency. A real-time experiment must pace audio or use live input, preserve one stream per selected track, and record monotonic send/receive times separately from audio offsets.

Streaming URLs contain credentials. Suppress complete URLs in HTTP/WebSocket debug logging and exception telemetry. Keep authentication in the documented query parameter rather than inventing header authentication. [Authentication guide](https://docs.modulate.ai/guides/authentication).

Streaming analysis windows can overlap. Compute coverage from the union of their time intervals; do not count overlapping windows as independent new speech or sum their durations to estimate submitted audio. This is our aggregation rule, separate from provider confidence.

A later live integration should enqueue a copy of selected PCM frames into a bounded partner worker. A slow or failed worker must never block the Twilio receiver. When queue overflow causes lost audio, mark the detector interval incomplete and restart the provider stream with a new local segment offset; do not silently compress time by removing dropped samples. Keep the recorder and ordinary call routing running. This is proposed partner behavior, not functionality already wired into Build 2.

## Handle failures as missing evidence

The adapter raises on all HTTP failures and malformed responses. The caller stores an error/unknown result, not a natural-speech verdict. The documented batch error classes include bad audio/request data, missing access, oversized input, short or invalid input, exhausted quotas, and inference failures. [Batch OpenAPI](https://docs.modulate.ai/api/velma_2_synthetic_voice_detection_batch.yaml).

| Condition | Partner behavior |
| --- | --- |
| HTTP `400`, `413`, `422` | Fix the input or clip selection; do not resend unchanged in a loop. |
| HTTP `401`/`403`, WS authentication/access rejection | Check the organization key and model access. |
| HTTP `429`, WS limit rejection | Distinguish concurrency from credits/monthly cap using the provider detail; pause work when the cause is not transient. |
| Network interruption, timeout, HTTP `5xx` | Record unknown; allow at most one delayed retry for this offline experiment, with jitter. |
| Missing `done`, unknown JSON shape, impossible values | Keep received evidence marked incomplete; fail the experiment visibly. |

The published default concurrency is three requests/connections per model, subject to organization settings. Start this project with one worker; increase only after checking the account. There is no basis here to assume POST retries are free or idempotent. [Authentication and limits](https://docs.modulate.ai/guides/authentication).

The human streaming reference groups some authentication and quota failures under `4003` and `4029`, while the downloadable AsyncAPI also lists `4001`, `4004`, `4030`, and `4031`. Accept that larger error set in the adapter; never interpret an unfamiliar close as success. An upgrade can fail before an error message arrives. [Streaming reference](https://docs.modulate.ai/api-reference/svd/streaming), [AsyncAPI](https://docs.modulate.ai/api/velma_2_synthetic_voice_detection_streaming.yaml).

## Price and timing assumptions

Published deepfake pricing is **$0.25 per processed audio hour** for both batch and streaming, checked 2026-09-26. A ten-second clip is about $0.000694 at that list rate; one minute is about $0.00417. These are arithmetic estimates, not a statement about account credits, billing increments, or taxes. [Official pricing](https://platform.modulate.ai/pricing#deepfake).

If ten-second batch clips are submitted every two seconds, roughly five times the source audio duration is processed, ignoring call edges. Prefer native streaming for continuous monitoring once validated, or deliberately use disjoint offline clips. Preserve a usage counter based on submitted duration and verify charges in the account dashboard.

Do not promise a first verdict at a fixed time from these docs. The [pricing page](https://platform.modulate.ai/pricing) and [FAQ](https://docs.modulate.ai/faq) describe 500 ms; the [benchmark page](https://docs.modulate.ai/benchmarks/deepfake-detection) describes 2.5 seconds and a different frame cadence. The capability guide describes expanding/sliding windows. Measure the endpoint actually used, with your account and transport, and consume explicit result timestamps rather than hardcoding its cadence.

The reviewed specifications do not give a contractual maximum streaming-session duration, a numeric per-organization production SLA, a general batch-duration ceiling separate from file size, or an immutable deployed-model identifier in the response. Do not invent values. The local examples have explicit clip and timeout bounds so these omissions do not block an initial offline experiment.

## Narrowband validation before relying on a verdict

Modulate's benchmark discussion explicitly says the arena does not measure narrowband telephony end to end. Therefore the advertised aggregate benchmark result is not an accuracy estimate for this Twilio deployment. Resampling an 8000 Hz recording to 16000 Hz cannot recover acoustic information absent from the recording; keep the original capture for reproducible comparisons. [Benchmark scope](https://docs.modulate.ai/benchmarks/deepfake-detection).

Run these five bounded evaluations before connecting any result to application behavior:

1. **Contract fixtures:** mock the HTTP response for all three verdicts, invalid confidence, missing fields, and provider errors. Assert that `no-content`, errors, and empty results never become “human confirmed.”
2. **Transport fixtures:** compare the same shareable speech clip as batch WAV and streaming PCM, including final `done` and intentional disconnect. Check timestamp offsets against the known source interval.
3. **Telephony fixtures:** pass labeled natural and synthetic test speech through the actual call path; include quiet speech, background noise, overlapping speech, and packet gaps. Keep speakers and source generators separated between tuning and evaluation sets.
4. **Track fixtures:** evaluate caller input independently. Evaluate caller playback as mixed playback evidence; separately label hold music, IVR prompts, and conferencing transitions. Do not attribute a playback verdict to a specific person.
5. **Failure isolation:** simulate unavailable credentials, full concurrency, no credits, slow responses, and worker overflow. The conversation continues; the evidence becomes unavailable or incomplete.

A synthetic-voice label describes acoustic provenance, not whether the speaker is dishonest, unauthorized, or harmful. This project should present the result as a review signal with its track and time range. The capability guide makes the same distinction. [Interpretation guidance](https://docs.modulate.ai/get-started/deepfake).
