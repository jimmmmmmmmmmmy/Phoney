# Detection alternatives: partner implementation recipes

Start with one completed Build 2 `inbound.wav` in a separate detector experiment environment. Implement one adapter, save its raw result, and evaluate it against matched human and generated examples before selecting a threshold.

**Status:** documentation and proposed examples only. These integrations are not installed in the running Twilio server. No detector API calls, model inference, accuracy measurements, or latency benchmarks were performed for this document. Provider contracts were checked against the linked primary sources on 2026-09-26.

Use [DEEPFAKE_DETECTION.md](DEEPFAKE_DETECTION.md) for the shared integration design and [PARTNER_HANDOFF.md](PARTNER_HANDOFF.md) for the existing replay interface. The providers below are alternatives to the main Modulate recipe, not dependencies that must all be enabled.

| Option | Best first experiment | Input adaptation | Score handling |
| --- | --- | --- | --- |
| Resemble Detect | Upload a short WAV; later try its documented streaming interface | Streaming recommends 16 kHz mono PCM16LE | Preserve the vendor label and synthetic score; higher indicates stronger synthetic signal |
| Reality Defender | Upload and poll from its Python SDK | WAV accepted; verify 8 kHz results rather than inventing a sample-rate guarantee | SDK normalizes ensemble score; retain status and nullable score |
| AASIST | Run the public checkpoint locally on a fixed test window | Resample to 16 kHz; 64,600-sample input | Upstream evaluation uses the bona-fide logit; its direction is opposite to a fake score |

Create a separate Python environment for experiments. Keep optional ML dependencies out of the telephony server's `requirements-lock.txt`. The examples below are standalone files to create in that environment, not existing repository commands.

## 1. Resemble Detect: hosted file analysis and streaming

### File experiment

Obtain a Resemble API key with Deepfake Detection access. The file endpoint accepts multipart uploads under `file`, including WAV, with a 150 MB direct-upload limit. Default submission is asynchronous; `Prefer: wait` requests a blocking result. The recipe below chooses asynchronous submission so it can retain the job ID and impose its own polling deadline. [Create Detection](https://docs.resemble.ai/detect/create), [first detection guide](https://docs.resemble.ai/guides/detect/first-detection)

Install `httpx` in the separate Python 3.11+ experiment environment. Set `RESEMBLE_API_KEY` in that process's environment and save this as `resemble_file.py`. This prototype submits a native Build 2 mono 8 kHz PCM16 WAV; that input choice does not claim measured detector accuracy on telephone audio.

```python
import asyncio
import json
import os
from pathlib import Path
import sys
from uuid import UUID
import wave

import httpx


async def read_json(client, method, url, **kwargs):
    async with client.stream(method, url, **kwargs) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_raw(chunk_size=65536):
            if len(body) + len(chunk) > 1024 * 1024:
                raise ValueError("Detection response exceeds the 1 MiB experiment limit")
            body.extend(chunk)
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Expected a detection response object")
    return payload


async def main():
    path = Path(sys.argv[1])
    if not path.is_file() or not 0 < path.stat().st_size <= 150_000_000:
        raise ValueError("Provide a nonempty WAV file of at most 150 MB")
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 8000):
            raise ValueError("This prototype expects mono 8 kHz PCM16 WAV")
        if source.getnframes() == 0:
            raise ValueError("WAV has no audio frames")
    base = "https://app.resemble.ai/api/v2/detect"
    headers = {"Authorization": "Bearer " + os.environ["RESEMBLE_API_KEY"],
               "Accept-Encoding": "identity"}
    async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=False) as client:
        async with asyncio.timeout(60):
            with path.open("rb") as audio:
                submitted = await read_json(
                    client, "POST", base,
                    files={"file": ("sample.wav", audio, "audio/wav")})
        if submitted.get("success") is not True:
            raise RuntimeError("Detection submission was not successful")
        job_id = str(UUID(submitted["item"]["uuid"]))
        print(json.dumps({"provider": "resemble", "job_id": job_id}), flush=True)
        try:
            async with asyncio.timeout(120):
                while True:
                    payload = await read_json(client, "GET", f"{base}/{job_id}")
                    if payload["item"].get("status") in {"completed", "failed"}:
                        print(json.dumps(payload))
                        return
                    await asyncio.sleep(2)
        except TimeoutError:
            raise TimeoutError(f"Polling deadline reached; retain job {job_id}") from None


asyncio.run(main())
```

Run `python resemble_file.py /absolute/path/to/test-window.wav`. Upload/response handling has a 60-second asynchronous deadline; all polling requests and sleeps share a 120-second deadline. Individual HTTP operations also time out. Responses are streamed with a 1 MiB cap and compression disabled. These are experiment limits, not provider latency promises. [HTTPX asynchronous response streaming](https://www.python-httpx.org/async/)

Preserve the returned job ID on timeout; a second upload creates another analysis rather than resuming the first one. If submission times out before returning an ID, record the outcome as unknown and inspect provider job history before retrying, because the provider may already have accepted it.

The Get Detection response places audio fields under `item.metrics`, including `label`, `score`, `consistency`, and `aggregated_score`. Examples contain numeric strings, so the adapter should preserve raw JSON and parse supported finite numeric values explicitly. Failed, missing, or malformed results become `unknown`, not human. Do not substitute `audio_source_tracing` for the authenticity verdict: it is a separate source-attribution result. [Get Detection](https://docs.resemble.ai/detect/get)

### Streaming adapter contract

Use `wss://stream.resemble.ai/api/v1/detect/audio` with `Authorization: Bearer …`. Wait for `ready`; send a PCM WAV header followed by binary mono 16 kHz signed PCM16LE, in roughly 100 ms blocks. Unknown-length WAV headers may use `0xFFFFFFFF` lengths. Read results concurrently; send text `{"type":"end"}` and wait for `final` before closing. Analysis windows are approximately four seconds; insufficient speech produces skipped windows. [Streaming contract](https://docs.resemble.ai/detect/streaming)

For current-window decisions, read `chunk_info.chunk_label` and `chunk_info.chunk_aggregated_score`; top-level metrics accumulate across analyzed windows. Higher aggregate scores mean a stronger synthetic signal, not a calibrated probability. Preserve source-time offsets from `chunk_info`. Null/skipped results are unknown. Connections have a 55-minute maximum and require entitlement/balance; `stream_id` cannot be polled as a persisted Detect UUID. [Streaming result semantics](https://docs.resemble.ai/detect/streaming)

Our proposed adapter should maintain one resampler and bounded outbound queue per call/track. Carry sample counters through resampling so event timestamps still refer to the original call. If the worker falls behind, report missing coverage and close that detector job; it must not slow the Twilio socket reader. A reconnect starts a new observation segment, so record its offset explicitly.

Do not copy the Twilio WebSocket protocol into this connection: Twilio sends JSON/base64 μ-law messages, while this detector expects binary PCM/WAV. Build 2's replay frames already contain PCM, so do not μ-law decode them again.

## 2. Reality Defender: upload, poll, preserve abstentions

Reality Defender documents an upload-and-result workflow with `X-API-KEY` authentication. The low-level API requests a presigned upload URL, uploads bytes with PUT, and polls `GET /api/media/users/{request_id}`. Its official SDK is the recommended starting point. This document does not claim that the public file API is a live audio WebSocket interface. [API quickstart](https://docs.realitydefender.com/api-reference/quickstart)

The SDK supports WAV and other audio containers up to 20 MB. The reviewed public contract does not specify an audio sample-rate requirement; confirm 8 kHz telephony performance with test fixtures or provider support. Convert a copy to 16 kHz only when the selected adapter requires it. [SDK formats and limits](https://docs.realitydefender.com/sdks/quickstart)

Install `realitydefender` in the experiment environment, record its installed version in your experiment manifest, and set `REALITY_DEFENDER_API_KEY`. Save this as `realitydefender_file.py`:

```python
import asyncio
import json
import os
from pathlib import Path
import sys

from realitydefender import RealityDefender


async def main():
    client = RealityDefender(api_key=os.environ["REALITY_DEFENDER_API_KEY"])
    try:
        uploaded = await asyncio.wait_for(
            client.upload(file_path=str(Path(sys.argv[1]).resolve())), timeout=60)
        request_id = uploaded["request_id"]
        print(json.dumps({"provider": "reality_defender", "request_id": request_id}), flush=True)
        result = await asyncio.wait_for(client.get_result(request_id), timeout=120)
        if result.get("status") not in {"MANIPULATED", "AUTHENTIC"} or result.get("score") is None:
            print(json.dumps({"decision": "unknown", "provider_result": result}))
            return
        print(json.dumps(result))
    finally:
        await asyncio.wait_for(client.cleanup(), timeout=5)


asyncio.run(main())
```

Run `python realitydefender_file.py /absolute/path/to/test-window.wav`. The SDK's documented `upload`, `get_result`, and `cleanup` methods are asynchronous. Event-based SDK callbacks still monitor a submitted job; they are not audio streaming. Keep the request ID when a local deadline expires. [Official Python SDK](https://github.com/Reality-Defender/realitydefender-sdk-python), [official example](https://github.com/Reality-Defender/realitydefender-sdk-python/blob/main/examples/basic_usage.py)

### Result normalization

Read the ensemble status first. Raw REST uses `FAKE`; the Python SDK maps that to `MANIPULATED`. Its ensemble `score` divides `resultsSummary.metadata.finalScore` by 100. A missing score remains `None`; individual model scores follow their own source fields. The SDK may return an in-progress status after its polling-attempt limit, so a successful HTTP/SDK return does not itself establish a completed verdict. [SDK result-processing source](https://github.com/Reality-Defender/realitydefender-sdk-python/blob/main/src/realitydefender/detection/results.py)

Map `MANIPULATED` to a synthetic indication and `AUTHENTIC` to a human indication only in the provider-specific adapter. Preserve `SUSPICIOUS`, `NOT_APPLICABLE`, `UNABLE_TO_EVALUATE`, unknown statuses, and missing evidence as abstentions. Do not turn a normalized number into a universal fake probability or mix it with another provider's raw score.

The media-detail contract documents audio abstentions for clips shorter than 1.5 seconds, dial tones/music, multiple speakers, noise, and unsupported language conditions. These are particularly relevant to the Build 2 outbound track, which can contain hold music and conference playback. Keep returned reasons with the observation. [Media-detail and audio reasons](https://docs.realitydefender.com/api-reference/endpoint/get_media_detail)

For an actionable first experiment, use a ten-second single-speaker window as a project default, not a claimed vendor minimum. Submit the same original and telephony-processed fixtures, compare applicability rates, and measure upload-plus-poll latency separately from audio-window length.

## 3. AASIST: reproducible local research baseline

The official NAVER repository publishes AASIST code and pretrained weights under its MIT license. It evaluates the ASVspoof 2019 LA benchmark; its reported benchmark result is not a performance guarantee for current voice generators or phone calls. The upstream command-line entry point requires CUDA and raises on CPU. For this Mac, a partner-owned inference wrapper can import the model directly and run on CPU; benchmark its latency before considering live use. [Official repository](https://github.com/clovaai/aasist), [upstream entry point](https://github.com/clovaai/aasist/blob/main/main.py), [license](https://github.com/clovaai/aasist/blob/main/LICENSE)

### Pin source and checkpoint

In the separate experiment directory:

```sh
git clone https://github.com/clovaai/aasist.git
git -C aasist checkout a04c9863f63d44471dde8a6abcb3b082b07cd1d1
python -m pip install 'torch>=2.1' numpy scipy soundfile
shasum -a 256 aasist/models/weights/AASIST.pth
```

The commit above was the official repository's `main` on the verification date. Save that commit, the checkpoint SHA-256 printed locally, and the resolved Python package versions with every evaluation. Use `config/AASIST.conf` with `models/weights/AASIST.pth`; AASIST-L has a different configuration/checkpoint. [Model configuration](https://github.com/clovaai/aasist/blob/main/config/AASIST.conf), [checkpoint](https://github.com/clovaai/aasist/blob/main/models/weights/AASIST.pth)

The architecture assumes 16 kHz mono audio. Upstream evaluation crops to the first 64,600 samples and repeats shorter input to fill the window. That is 4.0375 seconds at 16 kHz. Our proposed wrapper rejects shorter clips rather than treating repeated speech as extra evidence. Training labels are 0 for spoof and 1 for bona fide; evaluation exports the class-1 logit. [Audio loading and labels](https://github.com/clovaai/aasist/blob/main/data_utils.py), [16 kHz frontend](https://github.com/clovaai/aasist/blob/main/models/AASIST.py)

### Minimal CPU inference wrapper

Save as `aasist_window.py` beside the cloned directory. This is proposed integration code checked against the upstream interfaces; it has not been run against the checkpoint here. It deliberately prints raw scores and no binary decision.

```python
import json
from pathlib import Path
import sys

import numpy as np
from scipy.signal import resample_poly
import soundfile as sf
import torch

repository = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repository))
from models.AASIST import Model

audio, rate = sf.read(sys.argv[2], dtype="float32", always_2d=True)
if audio.shape[1] != 1 or rate not in (8000, 16000):
    raise ValueError("Provide a mono 8 kHz or 16 kHz WAV test window")
audio = audio[:, 0]
if not np.isfinite(audio).all():
    raise ValueError("Audio has non-finite samples")
if rate == 8000:
    audio = resample_poly(audio, 2, 1).astype(np.float32)
if len(audio) < 64600:
    raise ValueError("Insufficient audio: require at least 4.0375 seconds")
configuration = json.loads((repository / "config/AASIST.conf").read_text())
model = Model(configuration["model_config"]).cpu().eval()
weights = torch.load(repository / "models/weights/AASIST.pth",
                     map_location="cpu", weights_only=True)
model.load_state_dict(weights, strict=True)
tensor = torch.from_numpy(audio[:64600].copy()).unsqueeze(0)
with torch.inference_mode():
    _, logits = model(tensor)
print(json.dumps({"model": "AASIST", "window_seconds": 4.0375,
                  "spoof_logit": float(logits[0, 0]),
                  "bonafide_logit": float(logits[0, 1]),
                  "decision": "unconfigured"}))
```

Run `python aasist_window.py ./aasist /absolute/path/to/test-window.wav`.

The wrapper uses real resampling, not a WAV header change. `resample_poly(audio, 2, 1)` doubles the sample rate with filtering; it cannot recover frequencies removed by the telephone path. [SciPy resampling API](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.resample_poly.html), [SoundFile API](https://python-soundfile.readthedocs.io/en/latest/)

The upstream class-1 logit increases toward bona fide. If an adapter needs a synthetic-direction ranking, define a new field such as `spoof_logit - bonafide_logit` and calibrate it on held-out data. This is our proposed derived score, not the paper's exported metric. Softmax changes the scale but does not establish real-world probability calibration.

Load the model once per worker for repeated evaluation. Use a fixed window/hop policy and keep the original timestamps. Do not run a model load, checkpoint download, or inference inside a Twilio webhook. The existing replay consumer is an appropriate offline entry point; a future live worker requires its own bounded queue and timeout behavior.

### Evaluate the phone path before selecting any alternative

Use these five experiment gates for all three options:

1. **Separate the datasets by speaker, source recording, and generator.** Keep windows from one call in one split. Add at least one generator/model version absent from threshold tuning; otherwise similar fragments can make the result appear better than it is.
2. **Match the transmission path.** Compare clean originals, μ-law 8 kHz encode/decode copies, and actual consenting test calls. Apply the same path to human and synthetic classes. Preserve separate raw and transformed files; do not let container/sample-rate differences become class labels.
3. **Measure coverage and false alarms.** Report abstained windows, false positives on human calls, false negatives on generated calls, threshold choice, and per-call alarm rates. Keep uncertainty intervals and sample counts with the numbers; overlapping windows are correlated evidence.
4. **Exercise ordinary call audio.** Include silence, hold music, overlapping speech, quiet speakers, noise suppression, accents/languages, packet gaps, and short utterances. Report performance by condition, not just one aggregate accuracy.
5. **Measure end-to-end delay.** Include time gathering the window, resampling, queuing, upload, model execution, and polling. Test provider failure and slow workers. Detection failure must become unknown while the two-human call continues.

These are project evaluation requirements. For a research reference, ASVspoof 2021's LA task includes actual telephony/VoIP transmission conditions, including 8 kHz μ-law-related paths, while distributing evaluation audio at 16 kHz. That is a useful reminder that a file's sample rate does not establish its original bandwidth. [Challenge paper](https://www.isca-archive.org/asvspoof_2021/yamagishi21_asvspoof.pdf), [official evaluation resources](https://www.asvspoof.org/index2021.html)

**Scope of a result:** acoustic synthesis detection does not authenticate the caller's identity, establish who consented to a voice clone, or prove who wrote the spoken words. A recording of genuine speech can be replayed by someone else. A human can read machine-written text. Keep identity/authorship assertions outside this detector's contract.

**ElevenLabs classifier warning:** the public classifier is scoped to ElevenLabs-generated audio, uses the first minute, cannot classify other vendors' output, and currently warns that Eleven v3 detection is unreliable. A low match is not evidence that audio is human. It can be a supplementary ElevenLabs-specific experiment, not the general phone detector. [Official classifier scope](https://elevenlabs.io/ai-speech-classifier)
