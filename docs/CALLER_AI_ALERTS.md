# Caller AI alerts

The Call details heading shows one bright-red **AI Detected** flag when the caller has strong synthetic-speech evidence. There is no 50%/75% whole-call split. Unflagged, inconclusive, unavailable, and pending results show no badge. The dashboard does not display a numeric AI score or a separate analysis card. This acoustic flag does not establish identity or intent.

## Audio and classification

The incoming Twilio leg has two separate recordings. Only `inbound.wav` goes to detection: it contains the external caller's input. `outbound.wav` contains what that caller hears, including our operator and prompts, and is excluded. The combined stereo player remains unchanged. Channel separation does not remove acoustic echo from a caller's microphone.

Each saved provider interval contains its source stream, start/end milliseconds, verdict, and verdict confidence. The application partitions overlapping intervals so the same audio is counted once. Qualified speech takes precedence over weak or `no-content` windows. Weaker agreement cannot veto strong agreement. Conflicting qualified synthetic/non-synthetic results remain uncertain. Silence-only intervals contribute zero evidence, and missing audio is never treated as human speech.

Analysis policy version 3 sets `alert: ai_detected` once there are at least four seconds of strongly synthetic caller audio, regardless of how much natural speech or silence surrounds it. Qualifying provider confidence defaults to 0.80. These minimum-evidence rules prevent a weak or tiny fragment from triggering the flag; they are separate from the retired percentage tiers. Fewer than four seconds of synthetic evidence remains inconclusive. The backend retains speech-duration totals and the synthetic share for diagnostics, but the share no longer determines the flag.

The backend also returns merged `synthetic_intervals`, containing only strongly synthetic time ranges and their stream ID. Unaligned stream clocks cannot establish a flag or highlight. Live and partial positive results have a provisional tooltip and accessible label; completed recording results replace them.

## Transcript highlights

A finalized **Caller** paragraph receives a light-red background and a red robot SVG in its speaker metadata when its audio timestamps overlap a strong synthetic interval. The interval must belong to the same `stream_sid` as the transcript session. Operator speech, interim text, invalid timings, and mismatched stream epochs are not highlighted. Intervals that only touch a paragraph's start or end do not overlap it.

The whole paragraph marks an overlapping audio segment; it is not word-level classification. The robot's accessible label and tooltip explain that relationship. Deepgram transcript timestamps preserve the same initial silence and stream clock used by recordings, while batch evidence retains the trimmed capture-padding offset. No guessed timing adjustment is applied.

Detection-only polling updates the existing row annotations without replacing transcript rows, changing scroll position, or resetting playback. Playback highlighting and AI annotations remain separate. A transcript's percentage continues to mean **Transcription confidence**, never AI confidence.

## Automatic recorded-call analysis

Set both flags in the running server's private environment:

```dotenv
MODULATE_DETECTION_ENABLED=true
MODULATE_BACKFILL_ENABLED=true
```

The serial recording worker checks finalized caller WAVs, including historical calls. A new recording is uploaded as one continuous caller-only WAV, regardless of how much live audio was analyzed. Only declared initial capture padding is trimmed. The existing 30-minute capture limit bounds uploads below 29 MB, within the provider's 100 MB file limit. Modulate returns a timeline of short classifications across that complete file. Its 4–60 second recommendation is not a maximum upload duration.

The completed batch result replaces the provisional live classification; the two timelines are never mixed or double-counted. An unchanged recording with successful cached results is not submitted again after restart or merely because the aggregation policy changes. Jobs and attempts are stored privately beside the detection results. A process lock prevents the server and CLI from running the same jobs concurrently. Failures retry at most three times with backoff.

Version 1 jobs are archived before migration. Existing batch jobs whose ranges span the whole recording keep their paid results and retry budgets. Jobs that only reused live predictions or filled missing live coverage receive one full recording pass. Previously complete batch evidence is recalculated locally using the current policy, without another upload. Corrupt or mismatched cache files never silently authorize a new paid pass.

The worker waits for live calls, capture, detection, and pending result writes to finish. Candidate deployments do not run billable work; deployment draining waits for active work. Recordings with dropped/rejected media, internal timestamp gaps, invalid metadata, or excessive duration are not submitted. Only declared initial silence padding is trimmed, with its offset retained in saved intervals. Voicemail and fictional CRM conversations are not included.

Inventory and manual execution:

```bash
.venv/bin/python scripts/analyze_calls.py --env-file /absolute/private/path/.env
.venv/bin/python scripts/analyze_calls.py --env-file /absolute/private/path/.env --send-to-provider
```

Use `--call-sid CA...` to select one call. The command defaults to a dry run. There is no public dashboard endpoint that starts a provider request.

## Persistence and UI

Version 2 detection files include an `analysis` object with bounded intervals and recomputable duration totals. The analysis policy has its own version: policies 1 and 2 retain strict validation under their historical rules, while new calculations use policy 3. Version 1 detection files remain readable, but their old aggregate verdict cannot establish the new flag. The list API omits both raw windows and synthetic intervals except for the selected call. JSON exports retain the selected call's full evidence.

The only visible caller-analysis label is **AI Detected**, beside the Call details title and in the accessible descriptions of marked transcript segments. Backend diagnostics retain uncertainty, silence duration, source, coverage, and failure reasons.

Provider references: [Modulate aggregation guidance](https://docs.modulate.ai/get-started/voice-fraud-screening), [Modulate streaming](https://docs.modulate.ai/api-reference/svd/streaming), [Modulate batch](https://docs.modulate.ai/api-reference/svd/batch), and [Twilio track semantics](https://www.twilio.com/docs/voice/twiml/stream#track).

## Verification

Tests cover minimum synthetic evidence independent of whole-call share, qualified/weak/silent overlap, legacy policy validation, live progress, final full-recording analysis, cache migration, restart recovery, selected-call evidence, cache and WAV boundary validation, caller-only uploads, retries, candidate isolation, deployment draining, binary header flags, stream-aligned transcript annotations, and detection updates that preserve playback. Provider fakes establish application behavior; they do not establish detection accuracy.
