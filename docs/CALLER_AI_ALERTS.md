# Caller AI alerts

The Call details heading shows **AI Caller** when at least 75% of analyzed caller speech is confidently synthetic, and **Potentially AI** from 50% up to 75%. No badge is shown for an unflagged, inconclusive, unavailable, or pending result. The dashboard does not display a numeric AI score or a separate analysis card. These labels are acoustic advisories, not proof of identity or fraud. The 50%/75% thresholds are initial product settings to calibrate against labelled calls, not provider-defined accuracy levels.

## Audio and classification

The incoming Twilio leg has two separate recordings. Only `inbound.wav` goes to detection: it contains the external caller's input. `outbound.wav` contains what that caller hears, including our operator and prompts, and is excluded. The combined stereo player remains unchanged. Channel separation does not remove acoustic echo from a caller's microphone.

Each saved provider interval contains its source stream, start/end milliseconds, verdict, and verdict confidence. The application partitions overlapping intervals so the same audio is counted once. In analysis policy version 2, qualified speech takes precedence over weak or `no-content` windows. Weaker agreement cannot veto strong agreement. Conflicting qualified synthetic/non-synthetic results remain uncertain. Where no qualified speech exists, weak speech is uncertain; silence-only intervals contribute zero to both the numerator and denominator. Missing audio is unobserved, never treated as human speech. At least four seconds of reliable speech is required.

```
synthetic share = synthetic milliseconds /
                 (synthetic + non-synthetic + uncertain milliseconds)
```

The default qualifying provider confidence is 0.80. This is separate from the 50%/75% duration thresholds. Uncertain speech remains in the denominator; otherwise a tiny reliable fragment could misrepresent a mostly uncertain call. If uncertain speech could raise an otherwise unflagged result across the 50% threshold, the backend result is inconclusive and the UI shows no badge. Separate stream clocks cannot be combined as one timeline without a matching recording. Positive live or partial results have a provisional tooltip and accessible label.

## Automatic recorded-call analysis

Set both flags in the running server's private environment:

```dotenv
MODULATE_DETECTION_ENABLED=true
MODULATE_BACKFILL_ENABLED=true
```

The serial recording worker checks finalized caller WAVs, including historical calls. A new recording is uploaded as one continuous caller-only WAV, regardless of how much live audio was analyzed. Only declared initial capture padding is trimmed. The existing 30-minute capture limit bounds uploads below 29 MB, within the provider's 100 MB file limit. Modulate returns a timeline of short classifications across that complete file. Its 4–60 second recommendation is not a maximum upload duration.

The completed batch result replaces the provisional live classification; the two timelines are never mixed or double-counted. An unchanged recording with successful cached results is not submitted again after restart or merely because the aggregation policy changes. Jobs and attempts are stored privately beside the detection results. A process lock prevents the server and CLI from running the same jobs concurrently. Failures retry at most three times with backoff.

Version 1 jobs are archived before migration. Existing batch jobs whose ranges span the whole recording keep their paid results and retry budgets. Jobs that only reused live predictions or filled missing live coverage receive one full recording pass. Previously complete batch evidence is recalculated locally using policy version 2, without another upload. Corrupt or mismatched cache files never silently authorize a new paid pass.

The worker waits for live calls, capture, detection, and pending result writes to finish. Candidate deployments do not run billable work; deployment draining waits for active work. Recordings with dropped/rejected media, internal timestamp gaps, invalid metadata, or excessive duration are not submitted. Only declared initial silence padding is trimmed, with its offset retained in saved intervals. Voicemail and fictional CRM conversations are not included.

Inventory and manual execution:

```bash
.venv/bin/python scripts/analyze_calls.py --env-file /absolute/private/path/.env
.venv/bin/python scripts/analyze_calls.py --env-file /absolute/private/path/.env --send-to-provider
```

Use `--call-sid CA...` to select one call. The command defaults to a dry run. There is no public dashboard endpoint that starts a provider request.

## Persistence and UI

Version 2 detection files include an `analysis` object with bounded intervals and recomputable duration totals. The analysis policy has its own version: policy 1 is still strictly validated with the historical overlap rule, while new calculations use policy 2. Version 1 detection files remain readable, but their old aggregate verdict cannot establish the duration thresholds. The list API omits interval arrays except for the selected call. JSON exports retain the selected call's full evidence.

The only visible caller-analysis labels are **AI Caller** and **Potentially AI**, beside the Call details title. Backend diagnostics retain uncertainty, silence duration, source, coverage, and failure reasons. A transcript's separate confidence number is explicitly labeled **Transcription confidence**.

Provider references: [Modulate aggregation guidance](https://docs.modulate.ai/get-started/voice-fraud-screening), [Modulate streaming](https://docs.modulate.ai/api-reference/svd/streaming), [Modulate batch](https://docs.modulate.ai/api-reference/svd/batch), and [Twilio track semantics](https://www.twilio.com/docs/voice/twiml/stream#track).

## Verification

Tests cover exact duration thresholds, qualified/weak/silent overlap, legacy policy validation, live progress, final full-recording analysis, cache migration, restart recovery, selected-call evidence, cache and WAV boundary validation, caller-only uploads, retries, candidate isolation, deployment draining, and positive-only header badges. Provider fakes establish application behavior; they do not establish detection accuracy.
