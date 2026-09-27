# Manual phone agents

The Agents tab is a list of saved agents. **New agent** and each row open the same editor for name, personality prompt, ElevenLabs voice, and a unique `#1`–`#9` call shortcut. **Save agent** stores a versioned configuration; it does not start a call or activate an agent. Modulate cannot trigger takeover.

## Configure the installed server

The installed service reads `~/Library/Application Support/NewCollegeOperator/.env`, not this editing checkout's `.env`. Keep credentials and storage outside release directories.

| Setting | Purpose |
| --- | --- |
| `AGENT_MANAGEMENT_ENABLED=true` | Enable agent configuration. Requires existing `WORKSPACE_STORAGE_DIR`. |
| `AGENT_DEMO_MODE=true` | Let demo visitors read and save agents without a lock/unlock step. Writes require same-origin requests. Defaults false; does not grant access to paid voice enrollment or outbound/call-control APIs. |
| `GEMINI_API_KEY`, `ELEVENLABS_API_KEY` | Server-side providers. No keys or operator bearer token are sent to browser code. |
| `VOICE_OUTPUT_DIR` | Absolute private directory required by voice settings. Per-agent voices replace the optional global default voice ID. |
| `OWNER_NUMBER` | Owner's E.164 phone number, distinct from `TWILIO_NUMBER`. |
| `OPERATOR_ADMIN_TOKEN` | Random server-only token of at least 32 characters for protected operator APIs. |
| `VOICE_AGENT_ENABLED=true` | Allow explicitly selected agents to speak when providers and a ready published voice are available. Defaults false. |
| `OPERATOR_INBOUND_ENABLED=true` | Route new incoming calls through the agent-capable two-leg bridge. Requires voice flag, management, and transcription. Defaults false. |
| `ALLOWED_DESTINATIONS` | Comma-separated allowlist for the existing protected outbound API. Incoming calls do not expand this allowlist. |

Deploy the scaffold with `VOICE_AGENT_ENABLED=false` and `OPERATOR_INBOUND_ENABLED=false` until provider setup and a controlled phone test are ready. This keeps the current conference/voicemail route. When explicitly enabled, **only new calls** use the new transport; an existing conference cannot be converted mid-call. The new inbound bridge currently ends unanswered owner calls rather than using the conference voicemail flow.

There is no automatic-takeover setting. Enabling the manual feature never makes AI detection select an agent.

## Create or edit an agent

1. Open **Agents → New agent**, or select an existing row. The global **+ → New agent** opens the same popup.
2. Enter the name and personality prompt (up to 8,000 characters), choose a ready voice, and select an available `#1`–`#9` shortcut. **No shortcut** saves an unassigned agent.
3. Select **Save agent**. Calls pin the selected revision at activation; later edits apply to the next activation.

There are no Owner controls, voice catalog, or manual call controls on the Agents page. The demo deploy uses `AGENT_DEMO_MODE=true`, so visitors can edit executable agent settings. Actual takeover still requires the owner phone's keypad. Non-demo deployments keep the configuration API owner-session protected; the simplified demo UI has no unlock flow. The existing local `scripts/operator_access.py` tool and protected voice APIs remain available for administration.

On first demo startup with a ready voice named **owner**, the server seeds **Voice Clone**, shortcut **#1**, with personality **Tries to hang the call up asap**. A matching existing #1 agent is adopted rather than duplicated. A different agent already on #1 is retained without a shortcut. Seeding is recorded once and never resets later edits. It does not enroll a voice or make provider requests.

Agents, revisions, and voice mappings survive server restarts, deployments, and Cloudflare URL changes. The execution database is `agent-execution.sqlite3` in `WORKSPACE_STORAGE_DIR`, separate from the existing workspace database containing contacts and legacy drafts. The list includes legacy drafts, but execution records take precedence for matching IDs. Back up both databases with the app idle. Rollback can leave the execution database untouched.

## Use a manually enabled call

1. Call the Twilio number from a different phone. The caller hears exactly **New College Data Science** while `OWNER_NUMBER` rings. Answer normally: both microphones connect as soon as both signed audio streams are ready, with no acceptance digit. This starts a human conversation, not AI. Outbound API calls retain their separate press-1 acceptance step.
2. Talk normally, then press **#N** on the owner phone for the saved shortcut (for example, **#1** for Voice Clone). Remote-party keypad commands cannot activate agents.
3. Humans continue talking until the caller-only “An AI assistant is joining this call” announcement starts. Gemini prepares the reply while that announcement plays. The owner microphone is muted during the announcement and AI mode; the owner remains connected and hears the AI dialogue. Agent speech starts only after both the first reply audio is ready and Twilio acknowledges the announcement. Preparation is bounded to 15 seconds; failure returns to human relay.
4. Press **#0** at any point to cancel generation and queued playback and restore the owner microphone. Caller speech interrupts an agent answer and a finalized turn drives the next response.

The saved transcript supplies attributed context to Gemini; the published prompt remains separate from caller speech. Our agent's synthesized output is recorded for playback, represented as named `source=agent` transcript segments, and excluded from caller Modulate input. Only the remote microphone reaches caller AI detection. Interrupted agent phrases are labeled as interrupted; their full text is not proof that every word was heard. Acoustic speakerphone echo can still contaminate a remote microphone.

Both summary variants, contact matching, exports, and recent-call records use the canonical remote CallSid. Real leg StreamSids and reconnect generations are retained in recording provenance.

## Shared phone instructions and ending a call

Every agent uses the same internal Gemini instructions: it is on a phone call, receives speech-to-text transcripts, and speaks through ElevenLabs text-to-speech. The saved personality prompt adds behavior to these instructions rather than replacing them. Caller transcript text is conversation context, not a control command.

The runtime supplies the next agent reply number since this activation. Only complete, acknowledged replies advance this count; sentences, the joining announcement, earlier human dialogue, and interrupted replies do not. Repeating the same active shortcut preserves progress. Returning to human and selecting an agent again starts at reply one. Ordinary replies wait for fresh caller speech instead of consuming another turn from delayed pre-playback transcription. Turn-based personality instructions should specify what to do on each reply: “within three turns” allows an immediate hangup, whereas an explicit first/second/third-reply sequence describes the intended conversation.

The default phone model is **Gemini 3.5 Flash-Lite with MINIMAL thinking**. `GEMINI_MODEL` selects it independently of the post-call summary model. The adapter uses MINIMAL for stable 3.5/3.1 Flash-Lite, a zero thinking budget for stable 2.5 Flash/Flash-Lite, and LOW for other models including 3.8 Flash. A small three-reply provider check on September 27 measured 484–752 ms to first text for 3.5 Flash-Lite, with the end-call marker correctly emitted on reply three; this is not a latency guarantee. [Google's thinking settings](https://ai.google.dev/gemini-api/docs/generate-content/thinking).

HTTP connections are reused across replies and released at call end. No speculative response is generated before manual activation. A dummy request cannot guarantee that Google's hosted model worker stays warm; connection reuse avoids repeat handshakes, while the low-latency model addresses generation time. The agent has no calendar, messaging, or payment tools and must not claim to have performed those actions.

When the agent decides the conversation should end, it can say a brief farewell followed by the exact control marker `[/END CALL]` on its own final line. The stream parser handles markers split across Gemini chunks, removes the marker from synthesized speech, and accepts it only after a successful completed response. Quoted or inline mentions do not trigger it. The server waits for Twilio to acknowledge the caller's announcement and farewell playback before ending both call legs. A command-only response still plays the first-takeover announcement. Human return (`#0`), caller interruption, another shortcut, or provider failure cancels a pending hangup.

## Validation and limits

Offline tests use fake Twilio legs, Deepgram, Gemini, ElevenLabs, and Modulate. They cover inbound answer-to-connect in either stream order, privacy before both streams authenticate, outbound acceptance, manual-only activation, nine slots, immutable context/prompt/voice snapshots, announcement acknowledgements, realistic-length audio backpressure, interruption, `#0` races, failed transcription/provider recovery, explicit end-call parsing, farewell playback ordering, both-leg hangup, and late-dial/hangup cleanup. They do not establish real-phone audio quality, provider voice permissions, or production latency.

Before wider use, run one controlled phone call to verify caller-only cue, voice/context, repeated dialogue, interruption, `#0`, hangup, recording, and post-call summaries. Provider/STT failures restore human relay while the router remains healthy. A process/network failure cannot guarantee uninterrupted audio: this opt-in transport carries both microphones through Python. Deployment drains active calls; disabling inbound routing restores the original conference path for subsequent calls after restart/deploy.

### Handoff timing diagnostics

The installed `.runtime/app.log` retains `operator_trace` JSON events keyed by
`call_sid`. Each includes session-relative `elapsed_ms` and generation `epoch`.
The events distinguish shortcut receipt, preparation, Gemini's first text,
ElevenLabs' first audio, announcement playback, agent activation, caller
interruptions, and Twilio playback acknowledgments. Prompt text, transcript
text, audio, and provider credentials are excluded. Historical calls made
before these events were added cannot provide precise provider latency.

Human speech during preparation updates the history without repeatedly
canceling the handoff. The first prepared response plays after the announcement;
the next real caller response uses that updated history.
Only speech that starts after agent audio is sent can interrupt playback, so
provider generation time and delayed pre-playback STT are not barge-in events.
Announcement playback overlaps first-reply generation and synthesis; the fixed
announcement is cached by voice and model after a manual activation.
Repeated selection of the active shortcut is a no-op. `#0`, a different
shortcut, and hangup still cancel the current generation.
New finalized caller context defers an old end-call command until a fresh reply.
Generation traces include the selected agent revision, model, and reply number,
so a saved prompt can be tied to the activation that actually used it.
Provider HTTP failures log only the provider name and status code, never the
response body or credential-bearing URL.

If the tunnel drops and a terminal callback is missed, a bounded provider
status check reconciles an ended call. A socket disconnect alone never counts
as a hangup, and a reconnected stream invalidates the old check.
After a stream disconnects, inline TwiML waits eight seconds before requesting
fresh stream credentials. The reconnect webhook has two bounded connection/5xx
retries within Twilio's 15-second request limit, using
[Twilio connection overrides](https://www.twilio.com/docs/usage/webhooks/webhooks-connection-overrides).
This allows brief edge outages time to recover; it cannot prevent transport
loss or guarantee recovery during a sustained local-network/tunnel outage.
