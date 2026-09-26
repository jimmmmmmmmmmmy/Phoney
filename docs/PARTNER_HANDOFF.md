# Build 3 partner handoff — audio and transcripts

Open the [audio/transcript dashboard](BUILD_3.md), play/download a finalized local WAV, export a selected call as JSON/text, or replay one **completed local capture** with `scripts/replay_capture.py`. These interfaces let partners use recognized words or typed PCM without changing the phone bridge.

Build 3 keeps the working two-person conference and Build 2's two WAV tracks plus manifest. It adds live Deepgram STT with a public viewer, saved transcripts, and optional automatic Gemini summaries after calls. The code in `integrations/` still provides an offline-only audio contract and replay reader. Deepfake detection, Gemini dialogue, ElevenLabs voice cloning, prompt routing, and audio takeover remain partner work.

**Choose a partner implementation:** [deepfake detection](DEEPFAKE_DETECTION.md) specifies windowing, quality gates, provider adapters, results, evaluation, and later live integration. [Modulate](MODULATE.md) and [other detection options](DETECTION_ALTERNATIVES.md) supply concrete API contracts. [Gemini + ElevenLabs](VOICE_STACK.md) covers the separate conversational voice agent. You can write and test these adapters with synthetic/local fixtures while actual phone capture/transcription acceptance remains pending. Generated-audio integration checks do not replace that phone test.

## Use implemented transcript outputs

```sh
.venv/bin/python scripts/open_dashboard.py
```

The helper reads the installed service's configured public URL when its server-root pointer exists and opens `/dashboard`. Anyone with that ngrok URL can see caller numbers, call times/durations, saved summaries and transcript text, and play/download finalized local recordings. No viewer token, login, or authorization header is needed. Twilio signatures and deployment-control authentication remain in place for their separate routes.

| Route / artifact | Partner use |
| --- | --- |
| `GET /api/transcripts` | Read bounded metadata plus text for an automatically selected active/recent session. |
| `GET /api/transcripts?call_sid=<CallSid>` | Read selected-session finalized segments and current interim text. |
| `GET /api/transcripts/<CallSid>/export?format=json` | Download structured provider/model/format metadata plus the selected session. |
| Same export with `format=txt` | Download readable finalized speech. |
| `TRANSCRIPT_STORAGE_DIR/<CallSid>.json` | Private atomic finalized session on the server Mac. |

These HTTP routes are public and read-only. The list response always reports `selected_call_sid`; an absent or unavailable selector falls back to the first active session, otherwise the first recent session. Only that selected session contains segments and interim text. The JSON export envelope contains `schema_version`, `provider`, `model`, `sample_rate`, `track_meanings`, and `session`. Final segments contain `id`, `track`, `start_ms`, `end_ms`, `text`, and `confidence`. Join the session's `call_sid`/`stream_sid` to the capture manifest; retain both direction and timeline offsets. [Full API and status contract](BUILD_3.md#public-viewer-api).

Interim text may be replaced. Use final segments for durable downstream records, deduplicate by segment ID, and read session/track status before assuming coverage. A `completed` transcription remains a model prediction, not a guaranteed verbatim transcript. `storage_error` is separate from recognition status: a readable in-memory result can still have failed to save to disk.

The viewer holds ten recent finished sessions, while disk files have no automatic expiry. Transcription admits two concurrent calls, uses two independently bounded track streams per call, and reports capacity/overflow failures rather than slowing the humans. See [Build 3 limits](BUILD_3.md#limits-and-retention) before designing a live consumer. The separate replay tool below never contacts Deepgram.

The [voicemail placeholder](VOICEMAIL.md) adds a Twilio message recording after an unanswered call. Public `GET /api/voicemails` exposes bounded message metadata; the transcript snapshot includes the same data under `voicemail`. This metadata inbox works with Deepgram disabled. Its cloud recording is separate from local capture and is not exposed by the transcript API. Local whole-call WAVs can be played/downloaded separately through the recording library, including finalized partial captures. The same guide lists four useful provider-free partner tasks and a proposed structured message handoff.

## Caller details and summary handoff

The transcript snapshot's `call_details` object contains `enabled`, `storage_error`, and `calls`. Each call has `call_sid`, `caller_number`, `started_at`, `ended_at`, `duration_seconds`, and `summary`. A visible summary contains `text`, `source`, and `created_at`; Gemini results also carry their `model`. The source is `agent` for locally authored text or `gemini` for automatic results; otherwise the summary is null. Join by `call_sid`; JSON transcript exports include selected-call details under `call_details` when available. This separate details store intentionally publishes caller numbers; the existing raw audio manifest and provider transcript contracts are unchanged.

Automatic Gemini summaries use finalized timed text and completion status from ended calls, after the phone call is no longer active. Existing valid operator/agent-authored summaries are preserved. Both paths bind saved text to the transcript fingerprint and hide it when the transcript is unavailable or changed, so consumers must handle `summary: null` and an optional `summary_status` (`missing`, `pending`, `completed`, or `failed`). Partial/failed transcripts with finalized segments can be summarized with their missing coverage acknowledged.

Summary text names **Caller** and **New College DS** when attribution is clear; mixed playback or echoes must not become invented speaker assignments. The worker sends timed text and completion status only, without audio, CallSid, or separate caller metadata. It runs one provider request at a time and persists attempts across restarts; call routing, capture, and STT stay independent. Use [CALL_SUMMARIES.md](CALL_SUMMARIES.md) for configuration, retry behavior, and the manual CLI. No public endpoint creates or edits a summary, and Gemini voice-agent dialogue remains future work. After earlier HTTP 402 billing failures, a retry on September 26 generated and saved a Gemini summary from an existing ended transcript; the public API and dashboard displayed it. Existing authored summaries were preserved. A new real-phone capture/transcription/summary acceptance test remains pending.

## Consume recorded audio over HTTP

`GET /api/recordings` returns `{enabled, storage_error, recordings}` with the newest ten valid finalized captures among the first 1,000 directory entries scanned; newer captures beyond that scan are not guaranteed to appear. The transcript API includes the same snapshot under `recordings`. Use `GET` or `HEAD /api/recordings/<CallSid>/audio?track=combined|inbound|outbound` to retrieve audio. Byte-range requests support seeking.

The default `combined` response is stereo PCM16/8 kHz: left is caller input and right is caller playback. It is synthesized from the two stored mono WAVs without saving a third file; the shorter direction gets zero-padding. Individual `inbound`/`outbound` downloads preserve the original mono format. MP3/M4A exports are not implemented.

Public playback permits finalized `completed` and `partial` captures, with partial status visible. A published manifest and finalized valid WAV headers are required; active captures are not exposed. The offline replay API below still rejects partial captures. For detector evaluation, download original individual tracks, retain manifest quality/timing information from the local handoff, and do not silently mix the combined stereo channels into mono.

No file paths, arbitrary filenames, or Twilio cloud recording URLs are accepted by the public audio route. A voicemail cloud recording can exist without a playable local capture; its metadata status does not establish local audio availability. See [the recording API contract](BUILD_3.md#play-or-download-a-finalized-recording).

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

1. **Use the right input.** Start acoustic detection with completed PCM replay; start text consumers with finalized transcript exports. Capture-only mode and the offline replay runner require no provider credentials.
2. **Own your output contract.** Build 3 already exports transcript segments. Emit later detection results or agent decisions separately, keyed by call/session, stream, track, and timestamp; the switchboard does not consume those future decisions.
3. **Add new live consumers explicitly.** This synchronous replay protocol is not registered as a generic callback in FastAPI. The implemented Deepgram adapter uses its own bounded live path. Any new detector/agent consumer must likewise isolate slow or failed work from capture and the conference.
4. **Treat playback and takeover as a separate milestone.** Observing these files cannot send speech into the call, mute participants, or select prompts. The planned bidirectional bridge and command routes are described in [the implementation recipe](IMPLEMENTATION.md).
5. **Keep recordings out of Git.** Runtime captures are excluded from Git; the public dashboard intentionally serves finalized WAVs. Use locally generated or explicitly shared fixtures for partner tests; never commit a real call capture or provider credentials.

Verify this seam without any phone or provider account:

```bash
.venv/bin/python -m pytest tests/test_partner_scaffold.py -q
```
