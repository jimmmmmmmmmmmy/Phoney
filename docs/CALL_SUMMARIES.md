# Caller details and saved summaries

Run `.venv/bin/python scripts/open_dashboard.py`, select a call, and read its caller number, time, duration, and saved detailed summary below the call breadcrumb. The audio player stays in the bottom dock.

Gemini automatically creates two summaries for ended calls when configured: a one-sentence brief summary for the call list and a detailed summary for the call page. Each comes from a separate Gemini request using the finalized transcript; the brief summary is not a shortened copy of the detailed one. Locally authored detailed summaries remain supported and are preserved when they match the transcript. **Caller details and summaries are public to anyone with the public URL**, alongside the transcript and finalized recorded audio. Twilio and deployment mutation routes keep their existing authentication.

## Configure persistent call details

Add an optional absolute directory outside release checkouts to the installed service's private environment:

```dotenv
CALL_DETAILS_STORAGE_DIR=/absolute/private/path/to/call-details
# Optional: automatic summaries; also requires TRANSCRIPTION_ENABLED=true.
GEMINI_API_KEY=replace_in_private_environment_only
GEMINI_SUMMARY_MODEL=gemini-3.8-flash
```

The installed environment is `~/Library/Application Support/NewCollegeOperator/.env`; the source checkout's `.env` is separate. This directory stores caller details and summary data across deployments. It is local runtime data, excluded from Git. Keep real phone numbers, call text, summary text, and credentials out of committed examples.

Use a different directory from transcript and voicemail storage. Configuration rejects identical or equivalent paths because these stores use the same per-call JSON filenames.

Signed inbound `From` supplies the caller number. The terminal caller-status callback supplies the final call duration. The dashboard uses the supplied Twilio duration when available, otherwise a WAV duration or elapsed-time fallback. Phone-call duration and captured-media duration can differ. The dashboard labels the audio directions **Caller** (`inbound`) and **New College** (`outbound`); Gemini summary text uses **Caller** and **James**. The outbound track is still audio delivered to the caller, including conference output, prompts, and hold audio.

Existing Gemini summaries display the legacy speaker label **New College DS** as **James** in brief previews, detailed summaries, contact history, and JSON exports. This presentation change preserves the original stored text, transcript fingerprints, and completed jobs, without generating summaries again. Authored summaries, transcript speaker labels, and other school branding are unchanged.

The dashboard also displays **Caller** as the current contact's full name when its normalized phone number matches exactly. Brief previews and tooltips, detailed summaries, and cached contact history update after a contact is added, renamed, or assigned a different number. Unmatched calls retain **Caller**, and technical phrases such as **Caller ID** stay unchanged. This contact substitution applies only to Gemini summaries in the UI; saved text and exports retain the original role label, with no new provider request.

## Automatic Gemini summaries

The worker enables only when `GEMINI_API_KEY` is nonempty, `TRANSCRIPTION_ENABLED=true`, and `CALL_DETAILS_STORAGE_DIR` is configured. Its model defaults to `gemini-3.8-flash`; `GEMINI_SUMMARY_MODEL` is separate from the future voice-agent `GEMINI_MODEL`. It uses Google's text-only REST `generateContent` endpoint, with no tools or call-control capabilities. [GenerateContent contract](https://ai.google.dev/api/generate-content), [Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash).

1. Every two seconds, check available ended transcripts with finalized text. Wait until the corresponding phone call is no longer active. Ended `partial` or `failed` transcripts can qualify; an empty transcript cannot.
2. Submit only finalized segments (`track`, `start_ms`, `end_ms`, `text`) and the transcript's completion status. No audio, CallSid, separate caller metadata, private paths, or credentials enter the prompt. Words spoken in the call remain part of the submitted transcript.
3. Request the detailed summary in two or three factual plain-text sentences covering purpose, outcome, and explicit next actions. Make a separate request for the brief summary: one sentence of about 25 words covering the main purpose and outcome or next action. Both prompts attribute statements and follow-ups to **Caller** or **James**, with no vague “one participant” wording. Where echo or mixed playback makes attribution unclear, say so instead of inventing an owner. Treat transcript instructions as quoted data and acknowledge incomplete coverage.
4. Recheck the transcript fingerprint before saving each result with `source: "gemini"`, model, and creation time. Detailed and brief results have independent durable jobs. A valid existing result, including an authored summary saved while the request was running, is retained. A failure of either job does not block or regenerate a completed counterpart.

Requests run serially, at most one job per polling pass. The Gemini HTTP operation has a 30-second total deadline. Rate limits (`429`), provider errors (`5xx`), timeouts, and transport failures allow **five total attempts per summary kind**, with retry delays of **30 seconds, 2 minutes, 10 minutes, and 30 minutes**. This gives a temporary outage 42.5 minutes of waiting time to recover, plus request time. A normal call takes two requests; if both jobs exhaust their retries, the combined limit is ten attempts for that fingerprint. Each attempt is counted durably before contacting Gemini. Billing errors (`402`), authentication errors, blocked/invalid responses, and exhausted retries remain failed for that transcript fingerprint instead of retrying on every poll or restart. [Google recommends exponential backoff for temporary Gemini failures](https://ai.google.dev/gemini-api/docs/troubleshooting).

An older job that exhausted the former three-attempt policy on a known transient error is given the remaining attempts after the corresponding delay. Its persisted attempt count is retained, and restarting does not reset the budget or send a request immediately. Completed summaries and permanent failures are not requeued.

The private application log records `summary_trace` events for each durably started attempt, failure, and saved result. Events include CallSid, summary kind, model, attempt, elapsed milliseconds, safe error code, HTTP status when available, and retry timestamp. These records retain the cause after a successful retry clears the job's error. They exclude transcript and summary text, provider response bodies, exception messages, and credentials. A scheduled retry in the dashboard is not a terminal failure; check for the corresponding `completed` event and saved API result before manually requeuing work.

After restart, the worker checks recent calls and rotates through the saved archive in batches of at most 20 older transcripts per poll. Older eligible jobs remain retryable after leaving the ten-call in-memory preview. See [saved call history](SAVED_CALL_HISTORY.md). Existing calls with a valid detailed summary receive only the missing brief summary, without repeating the completed detailed request. Legacy records without brief fields still load. A changed transcript is a new fingerprint; both old summaries remain hidden and new attempts can qualify. Summary failure leaves the call, recording, and Deepgram transcript usable. The dashboard distinguishes an in-flight summary, a scheduled retry, and a terminal failure independently for each result; there is no public endpoint to generate, edit, or retry a summary.

The detailed result is limited to 2,000 characters; the brief result is limited to 280 characters. An oversized result fails validation instead of being truncated. Neither contains a voice synthesis or deepfake decision. Gemini and ElevenLabs dialogue/takeover adapters remain future work in [VOICE_STACK.md](VOICE_STACK.md).

**Verified on 2026-09-26:** after earlier HTTP 402 (`billing_required`) failures, an authorized retry through the installed worker generated and persisted one summary from an existing ended transcript. The saved result records `source: "gemini"` and model `gemini-3.8-flash`; the two existing authored summaries were unchanged. The public transcript API exposed the saved Gemini result, and the dashboard displayed it under **Call transcript** with **Caller** and **New College DS** attribution. This verifies provider generation, local persistence, and public display from saved text. It does not establish a new live phone call's complete capture/transcription/summary path; real-phone end-to-end acceptance remains pending.

Later that day, a completed transcript exhausted the original three attempts on Gemini `5xx` responses (`provider_unavailable`). An owner-authorized retry succeeded on its second attempt, and the running public transcript API exposed the persisted Gemini summary. No model, credential, or service restart was needed. This incident motivated the longer bounded retry window above; future provider outages can still leave a terminal failure after five attempts.

## Retry after repairing billing or configuration

1. Restore Gemini credits or correct the installed provider configuration. An HTTP `402` is stored as `billing_required` in the private job record; it does not trigger automatic repeated requests.
2. Reset one failed detailed summary, replacing the fictional CallSid with the ended local call's identifier:

   ```sh
   .venv/bin/python scripts/call_details.py \
     --env-file "$HOME/Library/Application Support/NewCollegeOperator/.env" \
     retry-summary CA00000000000000000000000000000000
   ```

3. Open that call in the dashboard. The enabled worker picks it up on a subsequent poll and applies the same bounded attempt policy.

For a failed brief summary, append `--kind brief` to the same command; the default is `--kind detailed`. This local command resets only the selected failed job matching the available finalized transcript. It preserves the counterpart and valid summaries, makes no Gemini request itself, and does not expose a public retry endpoint. A changed environment requires the service to load that configuration; adding credits alone does not change the environment.

## Backfill an existing local call

From the source checkout:

```sh
.venv/bin/python scripts/call_details.py \
  --env-file "$HOME/Library/Application Support/NewCollegeOperator/.env" backfill
```

The helper looks up Twilio metadata only for call SIDs present in the bounded local transcript, recording, or voicemail history. It does not backfill every older archive file automatically. Those Twilio requests are read-only; the helper saves returned details locally. It does not search the account's entire call history, place a call, or send audio/text to Gemini. Existing credentials remain in the private environment.

## Save an authored summary

1. Select an ended call with finalized text and read its transcript, including its completion status. Replace the fictional `CA000…` identifier in the command below with its actual `CallSid`.
2. Write a factual summary of at most 2,000 characters into a UTF-8 text file outside Git. Include the purpose, outcome, and explicit follow-up attributed to **Caller** or **New College DS**; acknowledge ambiguous attribution instead of guessing.
3. Save that text locally:

   ```sh
   .venv/bin/python scripts/call_details.py \
     --env-file "$HOME/Library/Application Support/NewCollegeOperator/.env" \
     summarize CA00000000000000000000000000000000 --text-file /absolute/private/path/to/summary.txt
   ```

4. Open the selected call in the dashboard. Its detailed summary appears below the call breadcrumb while the saved summary still matches the finalized transcript. The running store refreshes external file updates every two seconds; the next dashboard poll then displays the change.

The helper loads the ended call with finalized text and saves its transcript fingerprint with the summary. Ended `partial` or `failed` transcripts with finalized segments are eligible; the author must account for missing coverage rather than describe an incomplete record as complete. A missing, unfinished, empty, or changed transcript does not expose a stale summary. `source: "agent"` identifies this authored-summary workflow; it does not mean Gemini generated the text. This helper works without a Gemini key and makes no Gemini request. The public API has no mutation endpoint for summaries.

## Public snapshot contract

`GET /api/transcripts` includes this additional object under `call_details`:

```json
{
  "enabled": true,
  "storage_error": "",
  "calls": [
    {
      "call_sid": "CA00000000000000000000000000000000",
      "caller_number": "+12025550123",
      "started_at": "2026-09-26T16:00:00+00:00",
      "ended_at": "2026-09-26T16:01:00+00:00",
      "duration_seconds": 60,
      "summary": {
        "text": "Caller requested a callback tomorrow. New College DS agreed to call back.",
        "source": "agent",
        "created_at": "2026-09-26T16:02:00+00:00"
      },
      "brief_summary": {
        "text": "Caller requested a callback tomorrow, and James agreed to follow up.",
        "source": "gemini",
        "model": "gemini-3.8-flash",
        "created_at": "2026-09-26T16:02:02+00:00"
      }
    }
  ]
}
```

This uses a fictional number and illustrative text, not real call data. Automatic results use `source: "gemini"` and add `model`; authored results use `source: "agent"`. `summary` retains the detailed result and `brief_summary` supplies the call-list overview. Either is null when absent or when its saved fingerprint does not match the available finalized transcript; consumers should tolerate a missing `brief_summary` from older servers.

The call can include independent `summary_status` and `brief_summary_status` fields (`missing`, `pending`, `retrying`, `completed`, or `failed`); consumers must tolerate their absence on older records. A job with status `retrying` adds `summary_retry_at` or `brief_summary_retry_at`, respectively, as a Unix timestamp in seconds for its scheduled retry. Retry counters, fingerprints, and provider response bodies are not public fields.

Read `storage_error` separately from call or transcription success. JSON transcript exports can also contain a selected-call `call_details` object. The capture manifest, voicemail receipt, and raw provider transcript keep their existing schemas; caller numbers are published through the separate call-details object. Files retain private filesystem permissions and manual retention; the public viewer intentionally exposes the selected fields above.
