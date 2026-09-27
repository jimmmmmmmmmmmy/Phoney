# Modulate detection integration

The dashboard analyzes **new incoming calls' caller-input audio** with Modulate when `MODULATE_DETECTION_ENABLED=true`. The existing recording, Deepgram transcription, Gemini summaries, and operator controls continue independently. Results are advisory: detection cannot hang up, transfer, block, or answer a call.

## Imported work and audit

Source: [MuhammedAltindal-NCF/ShellHacks](https://github.com/MuhammedAltindal-NCF/ShellHacks), commit [`c94ef81ad80bfa5ff316b2603c014eca9849d194`](https://github.com/MuhammedAltindal-NCF/ShellHacks/commit/c94ef81ad80bfa5ff316b2603c014eca9849d194), from its `fictional-rotary-phone` directory. The source contains an older snapshot of this application. We imported its `partner_detection` adapters, worker, decision policy, explicit offline runners, and associated tests, then integrated their interfaces into the current application. We did not replace shared application files with that older snapshot.

The audit covered the detector, its test coverage and CLI tools, configuration, media lifecycle, dashboard contracts, persistence, and deployment isolation. Unrelated `floodguard`, Codex session logs, project PDFs, and duplicate assets were excluded.

| Finding in collaborator implementation | Integrated behavior |
| --- | --- |
| One 45-second timeout included the entire live call | Separate bounded audio collection and provider send/finalization deadlines |
| Reaching the audio cap discarded observations | Finish the analyzed portion normally and mark limited coverage |
| Timestamps ignored; missing audio could compress the timeline | Preserve initial offset; gaps/overlap return an inconclusive result |
| Reconnected streams reset the audio allowance | One shared per-call sample budget, including queued or failed submissions |
| Unbounded completed call history | Bounded active workers, stream epochs, history, audio, and result counts |
| Cancelled work left no outcome | Save an explicit inconclusive result |
| `non-synthetic` became `human` | Preserve synthetic/non-synthetic terminology; no identity claim |
| Very short evidence could produce a verdict | Require at least four seconds of qualifying speech intervals; this is a provisional project policy |
| Results existed only in process memory | Atomic private result files and restart recovery |
| No result display | Compact Voice analysis in the current call-detail view |
| Fork predates newer operator/CRM/summary changes | Additive integration preserves both summary outputs and current navigation |

## Configuration

```dotenv
MODULATE_DETECTION_ENABLED=true
# Also process finalized caller WAVs, including existing calls.
MODULATE_BACKFILL_ENABLED=true
MODULATE_API_KEY=your_private_key
DETECTION_STORAGE_DIR=/absolute/private/path/detection
# Shared across all stream attempts for a call.
MODULATE_DETECTION_MAX_AUDIO_SECONDS=120
MODULATE_DETECTION_DEADLINE_SECONDS=45
MODULATE_DETECTION_MIN_CONFIDENCE=0.80
MODULATE_DETECTION_QUEUE_FRAMES=250
```

Capture must already be enabled. Configuration validates the key, limits, and a storage directory distinct from recordings, transcripts, voicemail, and call details. A key by itself does not enable detection. The installed server reads its own private `.env`; copying a key into the source checkout alone does not configure the installed service.

Live media streams are analyzed when detection is enabled. Historical and newly finalized caller WAVs are processed only when `MODULATE_BACKFILL_ENABLED=true`. The private serial worker reuses valid live evidence and saved successful chunks; see [caller AI alerts](CALLER_AI_ALERTS.md) for duration thresholds, quality checks, and retry behavior. Candidate deployments cannot open provider connections: the active serving PID and commit must match the supervisor's record. Deployment drain waits for detector finalization and pending result writes.

The caller's validated inbound G.711 μ-law bytes are decoded to mono 8 kHz PCM16 and passed through a bounded, nonblocking detector queue. Outbound audio is excluded. A queue overflow, gap, failed transport, malformed result, insufficient speech, or conflicting evidence is inconclusive. Failure does not interrupt the human conference, local capture, or transcription.

## API and dashboard

The existing `/api/transcripts` polling response includes `detection.calls`; retained transcript sessions also include their matching `detection`. JSON exports include saved analysis when available. There is no public provider trigger or detector write endpoint. Detection follows the dashboard's existing URL-accessible visibility.

Results contain only bounded advisory metadata: provider, status, label, verdict confidence, reason, analyzed duration, coverage flag, counts, and update time. Credentials, raw provider messages, and audio are excluded. Files are atomic `0600` writes in private storage. A persisted unfinished analysis becomes inconclusive after restart. The detector store is separate from call details so rollback does not invalidate caller information or either Gemini summary.

The UI shows **AI caller** at 75% synthetic speech duration, **Potential AI caller** at 50–74%, **No AI speech flagged**, or **Inconclusive**. It never displays an exact AI percentage. Version 2 files preserve timestamped evidence and duration totals; version 1 aggregates remain readable but do not establish a duration-based alert. Limited and provisional coverage is stated explicitly. Transcription confidence remains separate.

## Offline checks

Both commands default to no-network dry runs. The explicit flag submits only the selected input:

```bash
python scripts/detect_audio.py /path/to/clip.wav
python scripts/detect_audio.py /path/to/clip.wav --send-to-provider
python scripts/detect_capture.py /path/to/manifest.json
python scripts/detect_capture.py /path/to/manifest.json --send-to-provider
```

The batch WAV contract is mono 8 kHz PCM16, 4–60 seconds. The replay runner rejects capture quality problems before transmission. It does not backfill the dashboard automatically.

Provider contracts were checked against the official [streaming reference](https://docs.modulate.ai/api-reference/svd/streaming) and [batch reference](https://docs.modulate.ai/api-reference/svd/batch). Streaming uses query-string authentication, raw `s16le`, and an empty text end marker; batch uses `X-API-Key` and `upload_file` multipart data. Never log authenticated WebSocket URLs.

Automated fake-provider tests establish transport, lifecycle, storage, and UI behavior. A generated non-speech tone can check live credentials and parsing but cannot establish speech detection accuracy. Representative natural and synthetic speech over the actual telephone channel remains the acceptance test for accuracy; the confidence threshold is provisional.

## Initial integration verification

The initial integration passed **1,278 tests**. Signed-media tests exercise a successful saved verdict and provider failure alongside Deepgram transcription, retained WAV capture, conference continuity, candidate isolation, deployment draining, and bounded shutdown. Desktop and 390×844 mobile previews verify the compact analysis section without horizontal overflow.

Both real Modulate endpoints accepted the configured key in an explicit connectivity check using a generated four-second non-speech tone. Batch returned `insufficient_audio` / `no-content`; streaming returned `no_usable_content`. No saved calls were uploaded during that initial connectivity check. This verifies connectivity and response parsing, not natural or synthetic speech detection accuracy.
