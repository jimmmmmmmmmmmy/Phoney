# Caller details and saved summaries

Run `.venv/bin/python scripts/open_dashboard.py`, select a call, and read its caller number, time, duration, and saved summary below the recording.

The current workflow saves operator/agent-authored summaries locally. It does not run Gemini automatically or require a Gemini API key. **Caller details and summaries are public to anyone with the ngrok URL**, alongside the transcript and finalized recorded audio. Twilio and deployment mutation routes keep their existing authentication.

## Configure persistent call details

Add an optional absolute directory outside release checkouts to the installed service's private environment:

```dotenv
CALL_DETAILS_STORAGE_DIR=/absolute/private/path/to/call-details
```

The installed environment is `~/Library/Application Support/NewCollegeOperator/.env`; the source checkout's `.env` is separate. This directory stores caller details and summary data across deployments. It is local runtime data, excluded from Git. Keep real phone numbers, call text, summary text, and credentials out of committed examples.

Use a different directory from transcript and voicemail storage. Configuration rejects identical or equivalent paths because these stores use the same per-call JSON filenames.

Signed inbound `From` supplies the caller number. The terminal caller-status callback supplies the final call duration. The dashboard uses the supplied Twilio duration when available, otherwise a WAV duration or elapsed-time fallback. Phone-call duration and captured-media duration can differ. The dashboard labels the two audio directions **Caller** (`inbound`) and **New College** (`outbound`). The latter is still audio delivered to the caller, including conference output, prompts, and hold audio.

## Backfill an existing local call

From the source checkout:

```sh
.venv/bin/python scripts/call_details.py \
  --env-file "$HOME/Library/Application Support/NewCollegeOperator/.env" backfill
```

The helper looks up Twilio metadata only for call SIDs present in the bounded local transcript, recording, or voicemail history. It does not backfill every older archive file automatically. Those Twilio requests are read-only; the helper saves returned details locally. It does not search the account's entire call history, place a call, or send audio/text to Gemini. Existing credentials remain in the private environment.

## Save an authored summary

1. Select an ended call with finalized text and read its transcript, including its completion status. Replace the fictional `CA000…` identifier in the command below with its actual `CallSid`.
2. Write a factual summary of at most 2,000 characters into a UTF-8 text file outside Git. Include the purpose, outcome, and any explicit follow-up; do not invent information absent from the transcript.
3. Save that text locally:

   ```sh
   .venv/bin/python scripts/call_details.py \
     --env-file "$HOME/Library/Application Support/NewCollegeOperator/.env" \
     summarize CA00000000000000000000000000000000 --text-file /absolute/private/path/to/summary.txt
   ```

4. Open the selected call in the dashboard. Its summary appears below the recording while the saved summary still matches the finalized transcript. The running store refreshes external file updates every two seconds; the next dashboard poll then displays the change.

The helper loads the ended call with finalized text and saves its transcript fingerprint with the summary. Ended `partial` or `failed` transcripts with finalized segments are eligible; the author must account for missing coverage rather than describe an incomplete record as complete. A missing, unfinished, empty, or changed transcript does not expose a stale summary. `source: "agent"` identifies this authored-summary workflow; it does not mean Gemini generated the text. The public API has no mutation endpoint for summaries.

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
        "text": "Example caller requested a callback tomorrow.",
        "source": "agent",
        "created_at": "2026-09-26T16:02:00+00:00"
      }
    }
  ]
}
```

This uses a fictional number and illustrative text, not real call data. `summary` is null when absent or when its saved fingerprint does not match the available finalized transcript. Read `storage_error` separately from call or transcription success. JSON transcript exports can also contain a selected-call `call_details` object. The capture manifest, voicemail receipt, and raw provider transcript keep their existing schemas; caller numbers are published through the separate call-details object.

## Add Gemini automation later

Keep the same transcript-bound storage contract when adding automatic summaries:

1. After an ended call has persisted finalized text, capture its fingerprint and submit that text plus its completion/coverage status to a Gemini summary adapter. Treat conversation text as data, not instructions that can change the summarizer's task.
2. Ask for a short purpose/outcome/follow-up summary, with no call-control tools. Bound response length and request duration. Provider failure leaves the summary absent and the call/transcript usable.
3. Before saving, confirm the transcript still has the same fingerprint. Persist through the existing summary store; never expose a result computed from an older transcript.
4. Test with local synthetic transcripts and a fake adapter before adding credentials or enabling provider calls. See [VOICE_STACK.md](VOICE_STACK.md) for the separately documented Gemini API contract.

This adapter is future work. The current local helper and saved summaries work without Gemini, ElevenLabs, or a detector.
