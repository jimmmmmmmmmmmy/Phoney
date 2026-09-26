```sh
.venv/bin/python scripts/open_dashboard.py
```

# Passive Operator

**Build 3:** the existing two-human Twilio call now has optional live **Deepgram Nova-3 transcription**, an HTML dashboard with recorded-audio playback, public JSON/text/WAV downloads, and saved transcript JSON with private filesystem permissions. The same media tap continues writing the two original WAV tracks. Open the viewer with the command above; it reads the installed server's configured public URL. Anyone with that ngrok URL can view live/saved text, play finalized local recordings, and download JSON/text/WAV without signing in. See [the Build 3 guide](docs/BUILD_3.md) for setup, output contracts, and acceptance checks.

**Current scope: Twilio calling, unanswered-call voicemail, capture, and observational speech-to-text.** Deepfake detection, Gemini dialogue, ElevenLabs voice cloning, and keypad takeover remain partner work. Partners can consume [saved transcripts and typed audio replay](docs/PARTNER_HANDOFF.md) while the people continue talking through the Twilio conference.

**Verification (2026-09-26):** all 400 automated tests pass, including finalized WAV playback, seeking, stereo alignment, and public-route checks. Chrome passed native playback, continued playback during polling, seeking to the end, and switching to an individual track using an isolated silent WAV fixture; no console warnings/errors were observed. The compact dashboard also passed Chrome checks for line highlighting, overlap, seek/track changes, persistent collapsible sections, and manual call selection during incoming live activity. This confirms player behavior, not recorded speech quality.

A browser check verified anonymous voicemail-inbox access using an isolated fake receipt, without placing a phone call or invoking a speech/agent provider. Twilio's `/voice` and caller-status `/status` POST webhooks were configured and verified by API read-back. Earlier generated speech passed a real Deepgram 8 kHz probe and a local signed-media/browser/export test with both WAV tracks completed. The live service has one completed capture with valid mono PCM16/8 kHz WAV headers (27.73 seconds inbound, 27.67 seconds outbound); this was a file/header check, not a listening test.

**Full real-phone capture, transcription, and voicemail acceptance remain pending.** Build 1's two-human phone audio and cleanup checks already passed. Check the deployed revision with `python3 scripts/server.py status` and `/health`; these checks do not establish that a new commit is deployed.

**Choose a guide:** [unanswered-call voicemail](docs/VOICEMAIL.md), [live transcript dashboard](docs/BUILD_3.md), [audio quality and VoIP](docs/AUDIO_QUALITY.md), [Gemini + ElevenLabs voice stack](docs/VOICE_STACK.md), or [deepfake detection implementation](docs/DEEPFAKE_DETECTION.md). The detector's [Modulate adapters](docs/MODULATE.md) and [alternative models/APIs](docs/DETECTION_ALTERNATIVES.md) are documented partner targets. Browse the [documentation index](docs/README.md) for all starting points.

## Partner starting point

1. Open the dashboard and select an available call. Play its finalized recording, download WAV audio, or export its transcript as JSON/text.
2. For acoustic work, open the matching completed capture under `MEDIA_STORAGE_DIR/<CallSid>/`; check `manifest.json` reports `status: completed`.
3. Run `.venv/bin/python scripts/replay_capture.py /absolute/path/to/manifest.json` for a local audio summary.
4. Implement `AudioConsumer.on_frame` / `on_end` using [the handoff examples](docs/PARTNER_HANDOFF.md), or consume the public transcript API for text. The replay tool itself makes no network requests.

`inbound.wav` and **Caller input** refer to the caller microphone. `outbound.wav` and **Caller playback** refer to what that caller heard, including the teammate, hold music, and prompts. Playback is not an isolated teammate microphone. WAVs remain PCM16 at 8 kHz; [switching an endpoint to VoIP does not increase Media Streams' export rate](docs/AUDIO_QUALITY.md).

Use external Chrome for the verified browser-player path; WAV download remains available. The audio player defaults to a combined stereo WAV: caller input on the left, caller playback on the right. Select either individual mono track when needed. Combined audio is generated from the two original files when requested, padding the shorter side with silence; no third file is stored. Playback waits for finalized WAV headers and a published `completed` or `partial` capture manifest. Partial recordings are labeled, and live capture is not served. This uses the existing 8 kHz PCM16 WAV format; MP3/M4A exports are not implemented.

The compact dashboard has collapsible Recent calls and Voicemail sections, always-on live updates, and transcript lines that highlight with WAV playback and seeking. Line timing comes from saved transcript segments; word-level timing is not inferred. Technical identifiers and provider statistics remain in the API/exports instead of dashboard labels.

Transcript JSON is saved with private filesystem permissions at `TRANSCRIPT_STORAGE_DIR/<CallSid>.json`; captures and transcripts stay outside Git and have no automatic deletion. Transcription supports two active calls at once, with two provider streams per call. Additional calls continue normally and show a visible transcription capacity failure. The dashboard retains ten recent finished sessions in memory/reloads; those history limits do not delete older disk files.

**Unanswered calls:** optional [voicemail](docs/VOICEMAIL.md) plays a team greeting and records a message only when the teammate has not connected. An established two-human conversation keeps its existing hangup behavior. The placeholder uses Twilio `Say`/`Record`; no Gemini, ElevenLabs, or detector requests are added. Real-phone voicemail acceptance remains pending.

**Final-build vision:** call someone through the operator, press `#1`, and let an agent using a clone of your own voice take over. `#2`, `#3`, and `#4` switch its saved prompts while the call continues. This is a modern version of being on hold: your AI representative keeps the conversation going while you step away. Outbound calls, inbound calls, voice enrollment, and returning control to the human are specified in [the final-build plan](docs/FINAL_BUILD.md). These features are planned, not yet implemented.

## Future product vision — partner integration

Imagine calling a car dealership. You start the conversation, explain which car you want, and press `#1` when you want to step away. An agent that sounds like you continues the same call with the context already discussed. Instead of elevator music, the other party has your AI representative to talk to.

The phone keypad becomes a prompt selector. Implement these defaults, configurable before the call:

| Command | Agent instructions |
| --- | --- |
| `#1` | Continue this conversation for me using its existing context. |
| `#2` | Handle the wait and notify me when I am needed. |
| `#3` | Complete my saved enquiry, such as asking the dealership for an itemized quote. |
| `#4` | Switch to another prompt I configured before the call. |

Changing a prompt can route the conversation to a different agent/model while preserving your cloned voice. `#0` returns the speaking role to you and interrupts the agent. The final product supports both inbound and outbound calls routed through the operator; manual takeover works whether the other party is human or AI.

### Future implementation reference

Use **Python/FastAPI + two Twilio bidirectional Media Streams + Deepgram + Google Gemini + ElevenLabs**. The Python bridge forwards the humans' audio until a keypad command substitutes the voice agent. For outbound calls, the server calls your phone first, you accept, and it calls the dealership. Both legs stay under the operator's control.

1. **Bridge the phones.** Add session state, owner acceptance, signed media WebSockets, and two-way audio forwarding.
2. **Prove takeover with a fixed clip.** Enroll your voice; make `#1` play a cloned phrase and `#0` interrupt it. Map the other keys to distinct profiles.
3. **Connect the agent.** Stream transcription → context and selected prompt → text model → cloned speech, with interruption and return-to-human handling.
4. **Handle phone menus and inbound calls.** Send IVR digits by updating only the remote call to play digits and reconnect its audio stream. Reuse the controller for incoming callers.
5. **Keep it running.** Add owner alerts, summaries, call cleanup, and deployment draining so a push waits for active calls to finish.

[**Implementation recipe →**](docs/IMPLEMENTATION.md) has exact modules, API routes, TwiML/Python examples, keypad parsing, audio mixing, timing policies, failure recovery, and a phone test for each stage. [**Voice provider adapters →**](docs/VOICE_STACK.md) has the actual cloning, STT, text-generation, and speech-streaming requests. [**Product behavior →**](docs/FINAL_BUILD.md) describes the dealership experience and acceptance criteria.

The design includes workarounds for the platform gaps: Python supplies the audio switch, Twilio call updates supply phone-menu digits, and FFmpeg supplies format conversion when needed. **These are later-build reference plans.** The implemented system is the conference bridge, optional unanswered-call voicemail, passive capture, and public Deepgram transcript viewer. Agent routing, cloned playback, and keypad controls still need implementation; partners can start with the audio and text outputs above.

## GitHub and the running server

Push changes to `main` in [fictional-rotary-phone](https://github.com/jimmmmmmmmmmmy/fictional-rotary-phone) to update the code running on the server Mac automatically. While idle, the server checks signed GitHub push events every second, with a 30-second polling fallback. Each revision must install, pass tests, and become healthy before it stays active. Read [the server guide](docs/SERVER.md) for setup, teammate access, status, and recovery.

The server Mac needs to be running, connected to the internet, and logged in. Repository writers can cause code to run with that Mac user's local file and credential access; invite trusted teammates only. A public repository link does not grant push access.

The installed service lives at `~/Library/Application Support/NewCollegeOperator`. Its `.env` is the active server configuration after installation; the project checkout remains available for editing. `python3 scripts/server.py status` shows the service location and deployed revision.

## Start locally

Allow about 5 minutes with Python, ngrok, and Twilio credentials ready.

These commands are for manual development. Use [server mode](docs/SERVER.md) for automatic deployment; both modes use port `8000`, so run one mode at a time.

1. Create the Python environment and install dependencies:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements-lock.txt
   ```

   `requirements-lock.txt` pins the verified environment, including tests. `requirements.txt` and `requirements-dev.txt` list the direct dependencies for intentional upgrades. Use Python 3.11 or newer.

2. Fill in `.env` using `.env.example` as the reference. Enable capture/transcription and set private provider/storage settings using [Build 3 configuration](docs/BUILD_3.md#configure-the-installed-server) when you want live text; both optional features can remain disabled for a basic bridge test. Set `CALLEE_NUMBER` to the teammate’s full E.164 number, different from `TWILIO_NUMBER`. Preserve existing credentials; `TWILIO_AUTH_TOKEN` validates callbacks. For automatic deployment, also set a random `DEPLOY_CONTROL_TOKEN` of at least 32 characters. Install ngrok and authenticate it if needed.

3. Start the app and tunnel:

   ```sh
   python3 scripts/dev.py start
   ```

   The helper starts `main:app` on port `8000`, starts ngrok, and saves the tunnel origin as `PUBLIC_BASE_URL` in `.env`.

4. Save the running tunnel’s webhook on the Twilio number:

   ```sh
   .venv/bin/python scripts/configure_twilio.py --apply
   ```

   This sets the number specified by `TWILIO_NUMBER` to `PUBLIC_BASE_URL/voice` for incoming calls and `PUBLIC_BASE_URL/status` for caller termination, both with method **POST**, and verifies both by read-back. Run without `--apply` to inspect settings without changing them. Repeat after a restart if the ngrok URL changes. The incoming voice URL is under the number’s **Voice → Handling for incoming calls**; the helper also configures the caller-status callback needed for hangup during the voicemail greeting.

5. Call the Twilio number from **a phone other than `CALLEE_NUMBER`**. Answer the teammate phone and exchange distinct phrases for 30 seconds. **Pass:** both people hear each other and either hangup ends both legs. Calls from the forwarding phone are rejected to prevent calling it back into itself. See the guide for no-answer and hangup tests.

Keep this computer awake while testing. The local app and ngrok must both remain running.

## Useful commands

| Action | Command |
| --- | --- |
| Open the public audio/transcript viewer | `.venv/bin/python scripts/open_dashboard.py` |
| Check processes and current webhook URL | `python3 scripts/dev.py status` |
| Stop managed app and tunnel | `python3 scripts/dev.py stop` |
| Run automated checks | `.venv/bin/python -m pytest` |
| Check local service health | `curl http://127.0.0.1:8000/health` |

An unsigned request to `/voice` is rejected. Browser visits and ordinary `curl` requests cannot stand in for a signed Twilio webhook. Validation uses the account Auth Token and the exact public URL; an API key secret is not a substitute. See [Twilio request validation](https://www.twilio.com/docs/usage/security).

Private process state and logs live in `.runtime/`. The configuration helper saves the prior Twilio settings there before applying a change. If startup fails, inspect `.runtime/app.log` or `.runtime/ngrok.log`.

## Build 3 boundary

This build provides the two-human conference, one fixed outgoing destination, optional unanswered-call voicemail, signed callbacks/media WebSockets, private local audio capture, optional live Deepgram transcription, a public HTML viewer with finalized local WAV playback/downloads, transcript exports, offline PCM replay, timeout cleanup, and deployment draining for outstanding work. It does not clone voices, detect deepfakes or conversational bots, generate replies, or implement an agent. Session state is in memory; explicit stop or a process crash ends continuity. The [server guide](docs/SERVER.md) covers operation and recovery.

The initial roadmap comes from [the shared Grok conversation](https://grok.com/share/bGVnYWN5_618ab7b3-9c27-4570-9709-edd7bee0bc21). The conference baseline is in [docs/BUILD_1.md](docs/BUILD_1.md); audio-capture checks are in [docs/BUILD_2.md](docs/BUILD_2.md), and current transcript checks are in [docs/BUILD_3.md](docs/BUILD_3.md). The expanded product direction is in [docs/FINAL_BUILD.md](docs/FINAL_BUILD.md).
