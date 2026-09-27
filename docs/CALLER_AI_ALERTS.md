# Caller AI alerts

Call details shows **AI caller** when at least 75% of analyzed caller speech is confidently synthetic, and **Potential AI caller** from 50% up to 75%. The dashboard does not display a numeric AI score. These labels are acoustic advisories, not proof of identity or fraud.

## Audio and classification

The incoming Twilio leg has two separate recordings. Only `inbound.wav` goes to detection: it contains the external caller's input. `outbound.wav` contains what that caller hears, including our operator and prompts, and is excluded. The combined stereo player remains unchanged. Channel separation does not remove acoustic echo from a caller's microphone.

Each saved provider interval contains its source stream, start/end milliseconds, verdict, and verdict confidence. The application partitions overlapping intervals so the same audio is counted once. Conflicting or low-confidence intervals count as uncertain; `no-content` intervals are excluded from the speech denominator. Missing audio is unobserved, never treated as human speech. At least four seconds of reliable speech is required.

```
synthetic share = synthetic milliseconds /
                 (synthetic + non-synthetic + uncertain milliseconds)
```

The default qualifying provider confidence is 0.80. This is separate from the 50%/75% duration thresholds. If uncertain speech could raise an otherwise unflagged result across the 50% threshold, the result is **Inconclusive**. Separate stream clocks cannot be combined as one timeline without a matching recording. Provisional alerts may appear during analysis; incomplete coverage is labeled visibly.

## Automatic recorded-call analysis

Set both flags in the running server's private environment:

```dotenv
MODULATE_DETECTION_ENABLED=true
MODULATE_BACKFILL_ENABLED=true
```

The serial recording worker checks finalized caller WAVs, including historical calls. It reuses valid live evidence for the same stream and analyzes missing coverage in bounded 4–60 second requests. An unchanged recording with saved successful results is not submitted again after restart. Jobs, attempts, and chunk results are stored privately beside the detection results. A process lock prevents the server and CLI from running the same jobs concurrently. Retries are bounded to three attempts with backoff.

The worker waits for live calls, capture, detection, and pending result writes to finish. Candidate deployments do not run billable work; deployment draining waits for active work. Recordings with dropped/rejected media, internal timestamp gaps, invalid metadata, or excessive duration are not submitted. Only declared initial silence padding is trimmed, with its offset retained in saved intervals. Voicemail and fictional CRM conversations are not included.

Inventory and manual execution:

```bash
.venv/bin/python scripts/analyze_calls.py --env-file /absolute/private/path/.env
.venv/bin/python scripts/analyze_calls.py --env-file /absolute/private/path/.env --send-to-provider
```

Use `--call-sid CA...` to select one call. The command defaults to a dry run. There is no public dashboard endpoint that starts a provider request.

## Persistence and UI

Version 2 detection files include an `analysis` object with bounded intervals and recomputable duration totals. Version 1 files remain readable, but their old aggregate verdict cannot establish the new duration thresholds; they appear inconclusive until recorded analysis supplies evidence. The list API omits interval arrays except for the selected call. JSON exports retain the selected call's full evidence.

The UI distinguishes **AI caller**, **Potential AI caller**, **No AI speech flagged**, **Inconclusive**, and an in-progress state. A transcript's separate confidence number is explicitly labeled **Transcription confidence**.

Provider references: [Modulate streaming](https://docs.modulate.ai/api-reference/svd/streaming), [Modulate batch](https://docs.modulate.ai/api-reference/svd/batch), and [Twilio track semantics](https://www.twilio.com/docs/voice/twiml/stream#track).
