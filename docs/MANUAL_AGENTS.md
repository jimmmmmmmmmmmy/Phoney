# Manual phone agents

The Agents tab now supports saved prompts, ElevenLabs voice selection or consented enrollment, and unique `#1`–`#9` assignments. Publishing a configuration does not start a call or activate an agent. Modulate cannot trigger takeover. Existing Calls, Contacts, recordings, résumé links, and workspace drafts remain in place.

## Configure the installed server

The installed service reads `~/Library/Application Support/NewCollegeOperator/.env`, not this editing checkout's `.env`. Keep credentials and storage outside release directories.

| Setting | Purpose |
| --- | --- |
| `AGENT_MANAGEMENT_ENABLED=true` | Enable protected publishing and voice management. Requires existing `WORKSPACE_STORAGE_DIR`. |
| `GEMINI_API_KEY`, `ELEVENLABS_API_KEY` | Server-side providers. No keys or operator bearer token are sent to browser code. |
| `VOICE_OUTPUT_DIR` | Absolute private directory required by voice settings. Per-agent voices replace the optional global default voice ID. |
| `OWNER_NUMBER` | Owner's E.164 phone number, distinct from `TWILIO_NUMBER`. |
| `OPERATOR_ADMIN_TOKEN` | Random server-only token of at least 32 characters. Browser controls use a separate owner session. |
| `VOICE_AGENT_ENABLED=true` | Allow explicitly selected agents to speak when providers and a ready published voice are available. Defaults false. |
| `OPERATOR_INBOUND_ENABLED=true` | Route new incoming calls through the agent-capable two-leg bridge. Requires voice flag, management, and transcription. Defaults false. |
| `ALLOWED_DESTINATIONS` | Comma-separated allowlist for the existing protected outbound API. Incoming calls do not expand this allowlist. |

Deploy the scaffold with `VOICE_AGENT_ENABLED=false` and `OPERATOR_INBOUND_ENABLED=false` until provider setup and a controlled phone test are ready. This keeps the current conference/voicemail route. When explicitly enabled, **only new calls** use the new transport; an existing conference cannot be converted mid-call. The new inbound bridge currently ends unanswered owner calls rather than using the conference voicemail flow.

There is no automatic-takeover setting. Enabling the manual feature never makes AI detection select an agent.

## Publish an agent

1. Open **Agents**, create or select a draft, and enter its name and prompt (up to 8,000 characters).
2. Generate a five-minute, single-use owner code locally, then paste it into **Unlock owner controls**:

   ```sh
   .venv/bin/python scripts/operator_access.py --env-file "$HOME/Library/Application Support/NewCollegeOperator/.env"
   ```

3. Use **Refresh voices** to load your account's catalog. Alternatively, expand voice enrollment and explicitly consent before uploading 1–3 voice samples (8 MiB each, 16 MiB total). A verification-required voice cannot be assigned until ready.
4. Choose a ready voice and a free `#1`–`#9` shortcut, then select **Publish call settings**. Calls pin this exact revision; later edits apply to the next activation.

Owner access uses a 12-hour Secure, HttpOnly, SameSite cookie. A tunnel URL change requires a fresh unlock, but contacts, drafts, published revisions, and voice mappings persist in server storage. `--revoke` on the same local command revokes owner sessions and pending codes. Public draft edits never silently replace an executable prompt.

The execution database is `agent-execution.sqlite3` in `WORKSPACE_STORAGE_DIR`, separate from the existing workspace database. Back up both with the app idle. Rollback can leave the execution database untouched.

## Use a manually enabled call

1. Call the Twilio number from a different phone. Answer on `OWNER_NUMBER` and press the prompted **1** to accept. Microphones stay private until acceptance.
2. Talk normally, then press **#N** on the owner phone, or choose the connected call and published agent in **Agents → Take over call**. Remote-party keypad commands cannot activate agents.
3. Humans continue talking during preparation. The caller alone hears “An AI assistant is joining this call.” The controller waits for Twilio's playback acknowledgement before agent speech starts. The owner remains connected and can hear the dialogue.
4. Press **#0** or **Return to human** at any point to cancel generation and queued playback and restore the owner microphone. Caller speech interrupts an agent answer and a finalized turn drives the next response.

The saved transcript supplies attributed context to Gemini; the published prompt remains separate from caller speech. Our agent's synthesized output is recorded for playback, represented as named `source=agent` transcript segments, and excluded from caller Modulate input. Only the remote microphone reaches caller AI detection. Interrupted agent phrases are labeled as interrupted; their full text is not proof that every word was heard. Acoustic speakerphone echo can still contaminate a remote microphone.

Both summary variants, contact matching, exports, and recent-call records use the canonical remote CallSid. Real leg StreamSids and reconnect generations are retained in recording provenance.

## Validation and limits

Offline tests use fake Twilio legs, Deepgram, Gemini, ElevenLabs, and Modulate. They cover acceptance privacy, manual-only activation, nine slots, immutable context/prompt/voice snapshots, announcement acknowledgements, realistic-length audio backpressure, interruption, `#0` races, failed transcription/provider recovery, and late-dial/hangup cleanup. They do not establish real-phone audio quality, provider voice permissions, or production latency.

Before wider use, run one controlled phone call to verify caller-only cue, voice/context, repeated dialogue, interruption, `#0`, hangup, recording, and post-call summaries. Provider/STT failures restore human relay while the router remains healthy. A process/network failure cannot guarantee uninterrupted audio: this opt-in transport carries both microphones through Python. Deployment drains active calls; disabling inbound routing restores the original conference path for subsequent calls after restart/deploy.
