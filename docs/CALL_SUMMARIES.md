# Caller details and saved summaries

Run `.venv/bin/python scripts/open_dashboard.py`, select a call, and read its caller number, time, duration, and saved summary directly under **Call transcript**. The audio player stays in the bottom dock.

Gemini automatically summarizes ended calls when configured. Locally authored summaries remain supported and are preserved when they match the transcript. **Caller details and summaries are public to anyone with the public URL**, alongside the transcript and finalized recorded audio. Twilio and deployment mutation routes keep their existing authentication.

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

Signed inbound `From` supplies the caller number. The terminal caller-status callback supplies the final call duration. The dashboard uses the supplied Twilio duration when available, otherwise a WAV duration or elapsed-time fallback. Phone-call duration and captured-media duration can differ. The dashboard labels the audio directions **Caller** (`inbound`) and **New College** (`outbound`); summary text uses **Caller** and **New College DS**. The latter is still audio delivered to the caller, including conference output, prompts, and hold audio.

## Automatic Gemini summaries

The worker enables only when `GEMINI_API_KEY` is nonempty, `TRANSCRIPTION_ENABLED=true`, and `CALL_DETAILS_STORAGE_DIR` is configured. Its model defaults to `gemini-3.8-flash`; `GEMINI_SUMMARY_MODEL` is separate from the future voice-agent `GEMINI_MODEL`. It uses Google's text-only REST `generateContent` endpoint, with no tools or call-control capabilities. [GenerateContent contract](https://ai.google.dev/api/generate-content), [Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash).

1. Every two seconds, check available ended transcripts with finalized text. Wait until the corresponding phone call is no longer active. Ended `partial` or `failed` transcripts can qualify; an empty transcript cannot.
2. Submit only finalized segments (`track`, `start_ms`, `end_ms`, `text`) and the transcript's completion status. No audio, CallSid, separate caller metadata, private paths, or credentials enter the prompt. Words spoken in the call remain part of the submitted transcript.
3. Ask for two or three factual plain-text sentences covering purpose, outcome, and explicit next actions. Attribute statements and follow-ups to **Caller** or **New College DS**, with no vague “one participant” wording. Where echo or mixed playback makes attribution unclear, say so instead of inventing an owner. Treat transcript instructions as quoted data and acknowledge incomplete coverage.
4. Recheck the transcript fingerprint before saving `source: "gemini"` with the model and creation time. A valid existing summary, including an authored summary saved while the request was running, is retained.

Requests run serially. The Gemini HTTP operation has a 30-second total deadline. Rate limits (`429`), provider errors (`5xx`), timeouts, and transport failures allow **three total attempts**, with 30-second and 120-second retry delays. Each attempt is counted durably before contacting Gemini. Billing errors (`402`), authentication errors, blocked/invalid responses, and exhausted retries remain failed for that transcript fingerprint instead of retrying on every poll or restart.

After restart, the worker catches up on eligible calls in the transcript manager's bounded recent history: up to ten restored transcripts. It does not scan the entire archive. A changed transcript is a new fingerprint; old summaries remain hidden and a new attempt can qualify. Summary failure leaves the call, recording, and Deepgram transcript usable. The dashboard shows a small pending or failed state; there is no public endpoint to generate, edit, or retry a summary.

The result is limited to 2,000 characters and contains no voice synthesis or deepfake decision. Gemini and ElevenLabs dialogue/takeover adapters remain future work in [VOICE_STACK.md](VOICE_STACK.md). The latest retry on September 26 again returned **HTTP 402**, stored as `billing_required`; the provider still reports a billing failure. The installed service uses the current configured key; successful generation is still blocked and unverified. The existing authored summaries are not Gemini-generated examples. Real-phone end-to-end acceptance also remains pending.

## Retry after repairing billing or configuration

1. Restore Gemini credits or correct the installed provider configuration. An HTTP `402` is stored as `billing_required` in the private job record; it does not trigger automatic repeated requests.
2. Reset one failed summary, replacing the fictional CallSid with the ended local call's identifier:

   ```sh
   .venv/bin/python scripts/call_details.py \
     --env-file "$HOME/Library/Application Support/NewCollegeOperator/.env" \
     retry-summary CA00000000000000000000000000000000
   ```

3. Open that call in the dashboard. The enabled worker picks it up on a subsequent poll and applies the same bounded attempt policy.

This local command resets only a failed job matching the available finalized transcript. It preserves valid summaries, makes no Gemini request itself, and does not expose a public retry endpoint. A changed environment requires the service to load that configuration; adding credits alone does not change the environment.

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

4. Open the selected call in the dashboard. Its summary appears directly under **Call transcript** while the saved summary still matches the finalized transcript. The running store refreshes external file updates every two seconds; the next dashboard poll then displays the change.

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
      }
    }
  ]
}
```

This uses a fictional number and illustrative text, not real call data. Automatic results use `source: "gemini"` and add `model`; authored results use `source: "agent"`. The call can also include `summary_status` (`missing`, `pending`, `completed`, or `failed`); consumers must tolerate its absence on older records. `summary` is null when absent or when its saved fingerprint does not match the available finalized transcript. Retry counters, fingerprints, and provider response bodies are not public fields.

Read `storage_error` separately from call or transcription success. JSON transcript exports can also contain a selected-call `call_details` object. The capture manifest, voicemail receipt, and raw provider transcript keep their existing schemas; caller numbers are published through the separate call-details object. Files retain private filesystem permissions and manual retention; the public viewer intentionally exposes the selected fields above.
