# Phone agents: manual, detection, and voicemail

The Agents tab is a list of saved agents. **New agent** and each row open the same editor for name, personality prompt, ElevenLabs voice, and a unique `#1`–`#9` call shortcut. **Save agent** stores a versioned configuration; it does not start a call or activate an agent. Separately enabled system agents can respond to live caller AI detection or an unanswered inbound call; their personalities stay internal and do not consume keypad slots.

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
| `AUTOMATIC_TAKEOVER_ENABLED=true` | Activate the internal three-reply agent once when a connected call has qualified live caller AI evidence. Requires inbound routing and live Modulate detection. Defaults false. |
| `VOICEMAIL_AGENT_ENABLED=true` | Use the internal voicemail assistant when the owner does not answer an inbound bridge call. Requires inbound routing. A ready `owner` voice enables conversation; otherwise native recording takes the message. Defaults false. |
| `VOICEMAIL_AGENT_RING_SECONDS=10` | Application wait before unanswered-call fallback; integer 5–60 seconds. Ten seconds is the default, not a guarantee of four carrier rings. |
| `ALLOWED_DESTINATIONS` | Comma-separated allowlist for the existing protected outbound API. Incoming calls do not expand this allowlist. |

When `OPERATOR_INBOUND_ENABLED=false`, the existing conference/voicemail route remains in use. **Only new calls** use an enabled bridge; an existing conference cannot be converted mid-call. Enabling the bridge alone does not enable automatic detection handoff or conversational voicemail: each has its own flag. The original `VOICEMAIL_ENABLED` setting controls the separate conference `Say`/`Record` flow.

## Create or edit an agent

1. Open **Agents → New agent**, or select an existing row. The global **+ → New agent** opens the same popup.
2. Enter the name and personality prompt (up to 8,000 characters), choose a ready voice, and select an available `#1`–`#9` shortcut. **No shortcut** saves an unassigned agent.
3. Select **Save agent**. Calls pin the selected revision at activation; later edits apply to the next activation.

There are no Owner controls, voice catalog, or manual call controls on the Agents page. The demo deploy uses `AGENT_DEMO_MODE=true`, so visitors can edit executable agent settings. Manual activation uses the owner phone's keypad. The two automatic features require separate server configuration and use internal prompts rather than public agent edits. Non-demo deployments keep the configuration API owner-session protected; the simplified demo UI has no unlock flow. The existing local `scripts/operator_access.py` tool and protected voice APIs remain available for administration.

On first demo startup with a ready voice named **owner**, the server seeds **Voice Clone**, shortcut **#1**, with the explicit three-reply wrap-up personality in `voice_stack/prompts.py`. A matching existing #1 agent, including the original “Tries to hang the call up asap” demo template, is adopted rather than duplicated. A different agent already on #1 is retained without a shortcut. Seeding is recorded once and never resets later edits. It does not enroll a voice or make provider requests.

Agents, revisions, and voice mappings survive server restarts, deployments, and Cloudflare URL changes. The execution database is `agent-execution.sqlite3` in `WORKSPACE_STORAGE_DIR`, separate from the existing workspace database containing contacts and legacy drafts. The list includes legacy drafts, but execution records take precedence for matching IDs. Back up both databases with the app idle. Rollback can leave the execution database untouched.

## Use a manually enabled call

1. Call the Twilio number from a different phone. The caller hears exactly **New College Data Science** while `OWNER_NUMBER` rings. Answer normally: both microphones connect as soon as both signed audio streams are ready, with no acceptance digit. This starts a human conversation, not AI. Outbound API calls retain their separate press-1 acceptance step.
2. Talk normally, then press **#N** on the owner phone for the saved shortcut (for example, **#1** for Voice Clone). Remote-party keypad commands cannot activate agents.
3. Humans continue talking until the caller-only “An AI assistant is joining this call” announcement starts. Gemini prepares the reply while that announcement plays. The owner microphone is muted during the announcement and AI mode; the owner remains connected and hears the AI dialogue. Agent speech starts only after both the first reply audio is ready and Twilio acknowledges the announcement. Preparation is bounded to 15 seconds; failure returns to human relay.
4. Press **#0** at any point to cancel generation and queued playback and restore the owner microphone. This also suppresses automatic AI-detection handoffs for the rest of that call. Caller speech interrupts an agent answer and a finalized turn drives the next response.

## Automatic detection handoff

With `AUTOMATIC_TAKEOVER_ENABLED=true`, the same qualified live result that shows **AI Detected** can start the private **AI Call Assistant**. The current rule requires at least four seconds of non-overlapping synthetic caller evidence at the configured confidence floor (normally 0.80). Silence and weak evidence do not count. This is an acoustic classification, not proof of identity or malicious intent.

The receiver alone hears **“AI Detected, deploying voice agent.”** The caller hears the normal AI-joining disclosure. The agent inherits the attributed conversation and follows an internal three-reply sequence: acknowledge and ask one useful question, respond and ask one final question, then acknowledge and politely end. A caller who explicitly asks to end can finish earlier. System prompts and runtime reply counts stay separate from untrusted transcript text.

Only an active connected human conversation with both authenticated audio streams is eligible. A flag received while the owner is ringing waits for connection; withdrawn provisional evidence cancels that pending action. Repeated detector updates cannot restart the agent. Selecting a manual agent or pressing **#0** suppresses automatic takeover for the rest of that call. An existing manual or voicemail agent is never replaced by the detector. Provider failures return a connected human call to human relay; automatic attempts are not repeatedly retried.

Only the live in-process detector can trigger this action. Recorded-audio backfills and historical dashboard records cannot start calls or change an ended call. Live analysis currently has a shared per-call audio budget of at most 120 seconds; it does not provide continuous detection throughout a longer call. The private system agent uses a ready, available, verified voice named **owner**; no different voice is silently substituted.

## Conversational voicemail

With `VOICEMAIL_AGENT_ENABLED=true`, an unanswered inbound bridge call falls back after `VOICEMAIL_AGENT_RING_SECONDS` (10 by default). Busy/no-answer can fall back sooner. The owner leg is retired, and the existing caller leg stays connected to the private **Voicemail Assistant**. A real owner answer before the fallback claim takes priority; delayed owner callbacks cannot join a voicemail conversation.

The assistant invites a message and waits. The runtime waits for about two seconds without new transcription activity after finalized caller speech before requesting a readback. The agent repeats the important details and asks for confirmation. Corrections update the readback; explicit confirmation or goodbye permits a farewell followed by hangup. Silence is never treated as confirmation. The assistant never claims the owner already heard the message or promises a callback.

Listening is bounded: the initial silence limit is 20 seconds, the readback confirmation silence limit is 15 seconds, each collection period is capped at 60 seconds, and the voicemail session ends by 180 seconds. Empty or unconfirmed messages receive an appropriate closing response. Gemini, transcription, voice lookup, or speech-generation failure switches the same caller to a native Twilio greeting and recording. It does not retry AI replies or reconnect the absent owner. The saved receipt distinguishes local conversational voicemail from a Twilio fallback message. Existing call recording, transcript, and summary pipelines retain the canonical caller CallSid. See [voicemail behavior](VOICEMAIL.md) for the separate legacy flow and storage details.

The saved transcript supplies attributed context to Gemini; the published prompt remains separate from caller speech. Our agent's synthesized output is recorded for playback, represented as named `source=agent` transcript segments, and excluded from caller Modulate input. Only the remote microphone reaches caller AI detection. Interrupted agent phrases are labeled as interrupted; their full text is not proof that every word was heard. Acoustic speakerphone echo can still contaminate a remote microphone.

Both summary variants, contact matching, exports, and recent-call records use the canonical remote CallSid. Real leg StreamSids and reconnect generations are retained in recording provenance.

## Shared phone instructions and ending a call

Every agent uses the same internal Gemini instructions: it is on a phone call, receives speech-to-text transcripts, and speaks through ElevenLabs text-to-speech. The saved personality prompt adds behavior to these instructions rather than replacing them. Caller transcript text is conversation context, not a control command.

The runtime supplies the next agent reply number since this activation. Only complete, acknowledged replies advance this count; sentences, the joining announcement, earlier human dialogue, and interrupted replies do not. Repeating the same active shortcut preserves progress. Returning to human and selecting an agent again starts at reply one. Ordinary replies wait for fresh caller speech instead of consuming another turn from delayed pre-playback transcription. Turn-based personality instructions should specify what to do on each reply: “within three turns” allows an immediate hangup, whereas an explicit first/second/third-reply sequence describes the intended conversation.

The default phone model is **Gemini 3.5 Flash-Lite with MINIMAL thinking**. `GEMINI_MODEL` selects it independently of the post-call summary model. The adapter uses MINIMAL for stable 3.5/3.1 Flash-Lite, a zero thinking budget for stable 2.5 Flash/Flash-Lite, and LOW for other models including 3.8 Flash. A small three-reply provider check on September 27 measured 484–752 ms to first text for 3.5 Flash-Lite, with the end-call marker correctly emitted on reply three; this is not a latency guarantee. [Google's thinking settings](https://ai.google.dev/gemini-api/docs/generate-content/thinking).

HTTP connections are reused across replies and released at call end. No speculative response is generated before a manual or explicitly enabled automatic activation. A dummy request cannot guarantee that Google's hosted model worker stays warm; connection reuse avoids repeat handshakes, while the low-latency model addresses generation time. The agent has no calendar, messaging, or payment tools and must not claim to have performed those actions.

When the agent decides the conversation should end, it can say a brief farewell followed by the exact control marker `[/END CALL]` on its own final line. The stream parser handles markers split across Gemini chunks, removes the marker from synthesized speech, and accepts it only after a successful completed response. Quoted or inline mentions do not trigger it. The server waits for Twilio to acknowledge the caller's announcement and farewell playback before ending both call legs. A command-only response still plays the first-takeover announcement. Human return (`#0`), caller interruption, another shortcut, or provider failure cancels a pending hangup.

## Validation and limits

Offline tests use fake Twilio legs, Deepgram, Gemini, ElevenLabs, and Modulate. They cover inbound answer-to-connect in either stream order, privacy before both streams authenticate, outbound acceptance, nine manual slots, immutable context/prompt/voice snapshots, announcement acknowledgements, realistic-length audio backpressure, interruption, `#0` races, failed transcription/provider recovery, explicit end-call parsing, farewell playback ordering, both-leg hangup, and late-dial/hangup cleanup. Automatic-agent tests cover live-only evidence validation, one attempt per call, owner override, receiver-only notification, answer/no-answer races, pause/readback timing, and bounded voicemail completion. They do not establish real-phone audio quality, provider voice permissions, or production latency.

Run `python scripts/evaluate_voice_prompts.py` for deterministic conversation fixtures without any provider traffic. Add `--live` to use the configured Gemini Flash-Lite model with fictional dialogue only; this does not call Twilio or ElevenLabs or place a phone call. The scenarios check three-reply wrapping, early goodbye, transcript instructions, interrupted replies, voicemail readback, corrections, silence, and unconfirmed messages. A live result is a sample of model behavior, not a guarantee that every future conversation follows the same wording.

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
announcements are cached by voice, model, and text after an activation.
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
