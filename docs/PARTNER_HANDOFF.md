# Build 2 partner handoff

Start with one **completed local capture** and `scripts/replay_capture.py`. This is the integration seam for partners: receive typed PCM frames without changing Twilio routing or making another phone call.

Build 2 keeps the working two-person conference. Its Twilio media tap writes two audio tracks and a manifest. The code in `integrations/` provides local contracts, a replay reader, and a metadata-only example. AI detection, transcription, voice-agent behavior, voice cloning, prompt routing, and audio takeover are partner work; none runs through this interface today.

**Choose a partner implementation:** [deepfake detection](DEEPFAKE_DETECTION.md) specifies windowing, quality gates, provider adapters, results, evaluation, and later live integration. [Modulate](MODULATE.md) and [other detection options](DETECTION_ALTERNATIVES.md) supply concrete API contracts. [Gemini + ElevenLabs](VOICE_STACK.md) covers the separate conversational voice agent. You can write and test these adapters with synthetic/local fixtures while the Build 2 phone-capture check remains pending.

## Run the local example

From the repository root, replace the manifest path with a completed capture on this computer:

```bash
.venv/bin/python scripts/replay_capture.py /path/to/completed/capture/manifest.json
```

The default command reads the audio in bounded chunks and prints one JSON summary containing duration, frame count, and track meanings. It does not print audio bytes, load `.env`, contact a provider, play sound, or place a call. The live server's captures belong to the installed server directory shown by `python3 scripts/server.py status`; the editable checkout can have a different runtime directory. On this Mac, each completed call is stored under:

```text
~/Library/Application Support/NewCollegeOperator/.runtime/recordings/<CallSid>/manifest.json
```

The directory comes from `MEDIA_STORAGE_DIR`. Capture is enabled with `MEDIA_CAPTURE_ENABLED=true`; `MEDIA_MAX_SECONDS` defaults to 1800 and accepts 1 through 3600. Reaching the capture limit stops recording, not the call. Captures have no automatic expiry: an operator removes recordings when no longer needed. Directories are private (0700) and capture files are private (0600).

For a frame-by-frame metadata example:

```bash
.venv/bin/python scripts/replay_capture.py /path/to/completed/capture/manifest.json --frames --realtime
```

`--realtime` paces by recorded timestamps; omit it to run as quickly as your consumer accepts frames. `--frame-ms` defaults to 20 and accepts integer values from 10 through 1000. The final frame of a track can be shorter. Replay frames are new chunks made from the WAV files; their count and boundaries need not match the original Twilio media messages.

## What each track actually contains

| Contract track | Meaning | What partners can assume |
| --- | --- | --- |
| `inbound` | `caller-input` | Audio Twilio received from the original inbound caller leg. |
| `outbound` | `caller-playback` | Audio Twilio sent to that caller: conference output, hold audio, and other playback on that leg. |

Both files use **8000 Hz, one channel, signed 16-bit little-endian PCM**. Twilio sends μ-law on the WebSocket; the capture writer decodes it before writing WAV. Replay supplies the decoded PCM bytes without a WAV header.

The outbound track is **not an isolated recording of the teammate's microphone**. During a two-person conversation it commonly contains the teammate's speech, but its stable contract is “what the caller heard.” Do not label it as a named speaker or assume it excludes hold audio. Partners needing independent participant microphones need another explicitly scoped capture stream in a later build.

WAV timelines start at media timestamp zero. The recorder fills missing time with zero samples. Replay timestamps therefore line up across both tracks, and `gap_samples` in each manifest track reports padded samples. A silent interval may be real silence or missing captured media; the current manifest reports the aggregate gap count, not a per-gap annotation. At equal timestamps, replay emits inbound first, then outbound. It does not mix the tracks.

## Implement a consumer

The interface is in [`integrations/contracts.py`](../integrations/contracts.py):

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True, slots=True)
class AudioFrame:
    session_id: str                 # Original inbound caller CallSid.
    stream_id: str                  # Twilio Media StreamSid.
    track: Literal["inbound", "outbound"]
    timestamp_ms: int               # Timeline offset, not wall-clock time.
    pcm_s16le: bytes                # Mono signed PCM16, little-endian.
    sample_rate: int = 8000
    channels: int = 1
```

The exported implementation validates these values and exposes `frame.sample_count`. Audio bytes are omitted from the frame's default representation.

A partner module only needs `on_frame(frame)` and `on_end(capture)`. Here is a complete local counter:

```python
from integrations.contracts import AudioFrame, CaptureFinished
from integrations.replay import replay_capture

class PartnerCounter:
    def __init__(self):
        self.samples = {"inbound": 0, "outbound": 0}

    def on_frame(self, frame: AudioFrame) -> None:
        self.samples[frame.track] += frame.sample_count
        # Partner implementation goes here. This example retains no audio.

    def on_end(self, capture: CaptureFinished) -> None:
        print({"samples": self.samples, "frames": capture.frame_count})

replay_capture("/path/to/completed/capture/manifest.json", PartnerCounter())
```

`CaptureFinished` contains `session_id`, `stream_id`, `manifest_path`, and `frame_count`. Its callback means this local replay finished successfully; it is not a live Twilio call-status event. Consumers run sequentially in the replay process. Exceptions stop replay and propagate to the caller; a failed replay does not invoke `on_end`. Keep a separate completion/error state if your partner implementation batches work asynchronously.

For a reusable metadata observer, see [`integrations/example_observer.py`](../integrations/example_observer.py). `NullConsumer` is the default: it retains no audio and has no external side effects.

## Capture manifest contract

Schema version 1 is published after the recorder closes both WAV files. The replay reader accepts only `status: "completed"`; active, partial, and failed captures remain available for local diagnosis but are not valid partner replay inputs.

```json
{
  "schema_version": 1,
  "status": "completed",
  "finish_reason": "stream-stopped",
  "call_sid": "CA…",
  "stream_sid": "MZ…",
  "account_sid": "AC…",
  "started_at": "2026-09-26T12:00:00+00:00",
  "finished_at": "2026-09-26T12:00:30+00:00",
  "sample_rate": 8000,
  "channels": 1,
  "sample_width": 2,
  "encoding": "pcm_s16le",
  "tracks": {
    "inbound": {
      "file": "inbound.wav",
      "meaning": "caller-input",
      "frames": 1500,
      "samples": 240000,
      "gap_samples": 0,
      "first_timestamp_ms": 0,
      "last_timestamp_ms": 29980,
      "last_chunk": 1500
    },
    "outbound": {
      "file": "outbound.wav",
      "meaning": "caller-playback",
      "frames": 1500,
      "samples": 240000,
      "gap_samples": 0,
      "first_timestamp_ms": 0,
      "last_timestamp_ms": 29980,
      "last_chunk": 1500
    }
  },
  "counters": {
    "media_messages": 3000,
    "rejected_messages": 0,
    "dropped_messages": 0
  }
}
```

The values above illustrate the schema; they are not a claim that a real call was captured. `samples` includes padding and equals the WAV header's sample count. `frames` counts captured media messages for that track. An empty track has zero samples and null first/last timestamp fields. Consumers should tolerate additional manifest keys within version 1.

[`integrations/replay.py`](../integrations/replay.py) checks both WAV headers before emitting the first frame, limits manifests to 64 KiB, limits audio to two hours per track, verifies declared sample counts, and rejects WAV paths outside the capture directory or symlink WAVs. It reads one small frame per track at a time and checks for truncation while reading. These are reader bounds; they do not override the live recorder's shorter duration or storage limits.

## Partner work boundaries

1. **Develop against replay first.** Write a consumer and use a completed capture. Keep provider configuration inside your own module; the recorder and replay runner require no AI credentials.
2. **Own your output contract.** Emit any later transcript, detection result, or agent decision separately, keyed by `session_id`, track, and timestamp. Those outputs are not implemented or consumed by the switchboard in Build 2.
3. **Add live integration as an explicit later change.** This synchronous replay protocol is not registered in the FastAPI server. A future live adapter must use bounded queues and isolate slow or failed consumers from the Twilio audio path.
4. **Treat playback and takeover as a separate milestone.** Observing these files cannot send speech into the call, mute participants, or select prompts. The planned bidirectional bridge and command routes are described in [the implementation recipe](IMPLEMENTATION.md).
5. **Keep recordings local.** Runtime captures are excluded from Git. Use locally generated or explicitly shared fixtures for partner tests; never commit a real call capture or provider credentials.

Verify this seam without any phone or provider account:

```bash
.venv/bin/python -m pytest tests/test_partner_scaffold.py -q
```
