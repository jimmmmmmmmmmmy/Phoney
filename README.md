```sh
.venv/bin/python scripts/open_dashboard.py
```

# Passive Operator

**Build 3:** the existing two-human Twilio call now has optional live **Deepgram Nova-3 transcription**, a minimal dashboard with caller number/time/duration, recorded-audio playback, automatic Gemini summaries after calls, public JSON/text/WAV downloads, and transcript JSON stored with private filesystem permissions. The same media tap continues writing the two original WAV tracks. Open the viewer with the command above; it reads the installed server's configured public URL. Anyone with that public URL can see caller numbers, call times/durations, saved summaries, and transcript text, play finalized local recordings, and download JSON/text/WAV without signing in. See [the Build 3 guide](docs/BUILD_3.md) for setup, output contracts, and acceptance checks.

**Current scope: Twilio calling, unanswered-call voicemail, capture, observational speech-to-text, Gemini summaries after calls, and a standalone voice layer that answers a question and speaks the reply in an enrolled voice.** Deepfake detection, keypad takeover, and the in-call voice relay remain partner work, because nothing in `app.py` imports the voice layer yet: it is verified from the command line rather than inside a live call. Partners can consume [saved transcripts and typed audio replay](docs/PARTNER_HANDOFF.md) while the people continue talking through the Twilio conference.

**Verification baseline (2026-09-26):** 839 automated tests passed. The voice adapters are checked offline through `MockTransport` without provider requests. Chrome checks with silent synthetic recordings verified browser Back/Forward, playback continuing during polling and collection/page navigation, seeking with transcript highlighting, and resetting playback when opening another call. Desktop, 390×844 mobile, and 844×390 landscape checks verified that search, navigation, and playback stay in place while content scrolls. No browser warnings or errors were reported. Earlier fixture checks also covered overlapping speech. These checks do not establish recorded speech quality. The CRM update was also checked in Chrome on desktop and mobile: required-field validation, adding all optional contact fields, browser-local contact and agent persistence after reload, contact search, and matching saved calls by phone number. Demo conversations do not include recordings. Desktop and mobile checks verified caller breadcrumbs, filtering and browser history without interrupting playback, plus centered header creation controls.

A browser check verified anonymous voicemail-inbox access using an isolated fake receipt, without placing a phone call or invoking a speech/agent provider. Twilio's `/voice` and caller-status `/status` POST webhooks were configured and verified by API read-back. Earlier generated speech passed a real Deepgram 8 kHz probe and a local signed-media/browser/export test with both WAV tracks completed. The live service has one completed capture with valid mono PCM16/8 kHz WAV headers (27.73 seconds inbound, 27.67 seconds outbound); this was a file/header check, not a listening test.

**Gemini generation verified (2026-09-26):** after earlier HTTP 402 billing failures, an authorized retry succeeded through the installed summary worker. It generated and saved one summary from an existing ended transcript with `source: "gemini"` and model `gemini-3.8-flash`. The public API and dashboard displayed the saved summary under **Call transcript**, naming **Caller** and **New College DS**. The two existing authored summaries were unchanged. **Full real-phone capture, transcription, and voicemail acceptance remain pending.** Build 1's two-human phone audio and cleanup checks already passed. Check the deployed revision with `python3 scripts/server.py status` and `/health`; these checks do not establish that a new commit is deployed.

**Choose a guide:** [caller details and saved summaries](docs/CALL_SUMMARIES.md), [unanswered-call voicemail](docs/VOICEMAIL.md), [live transcript dashboard](docs/BUILD_3.md), [audio quality and VoIP](docs/AUDIO_QUALITY.md), [Gemini + ElevenLabs voice stack](docs/VOICE_STACK.md), or [deepfake detection implementation](docs/DEEPFAKE_DETECTION.md). The detector's [Modulate adapters](docs/MODULATE.md) and [alternative models/APIs](docs/DETECTION_ALTERNATIVES.md) are documented partner targets. Browse the [documentation index](docs/README.md) for all starting points.

## Partner starting point

1. Open the dashboard and select an available call. Play its finalized recording, download WAV audio, or export its transcript as JSON/text.
2. For acoustic work, open the matching completed capture under `MEDIA_STORAGE_DIR/<CallSid>/`; check `manifest.json` reports `status: completed`.
3. Run `.venv/bin/python scripts/replay_capture.py /absolute/path/to/manifest.json` for a local audio summary.
4. Implement `AudioConsumer.on_frame` / `on_end` using [the handoff examples](docs/PARTNER_HANDOFF.md), or consume the public transcript API for text. The replay tool itself makes no network requests.

`inbound.wav` and **Caller input** refer to the caller microphone. `outbound.wav` and **Caller playback** refer to what that caller heard, including the teammate, hold music, and prompts. Playback is not an isolated teammate microphone. WAVs remain PCM16 at 8 kHz; [switching an endpoint to VoIP does not increase Media Streams' export rate](docs/AUDIO_QUALITY.md).

The audio player stays in a fixed dock at the bottom of the page and plays the combined stereo WAV: caller input on the left, caller playback on the right. Its native controls support seeking, and the download saves that combined WAV. Individual mono tracks remain available through the recording API for partner use. Combined audio is generated from the two original files when requested, padding the shorter side with silence; no third file is stored. Playback waits for finalized WAV headers and a published `completed` or `partial` capture manifest. Partial recordings are labeled, and live capture is not served. This uses the existing 8 kHz PCM16 WAV format; MP3/M4A exports are not implemented.

Call rows show the caller number, time, and duration. The selected call shows its saved summary directly under **Call transcript**, followed by lines labeled **Caller** and **New College**. Its audio stays available in the bottom dock. Live text and playback highlights scroll automatically. With `GEMINI_API_KEY`, transcription, and call-details storage configured, Gemini automatically summarizes ended calls from finalized text. Summaries name **Caller** and **New College DS** when attribution is clear. Existing valid authored summaries are retained; changed or missing transcripts hide stale summaries. [Configure summaries or save one locally](docs/CALL_SUMMARIES.md). The default summary model is `gemini-3.8-flash`; this runs after calls and does not speak on them.

The dashboard uses one global navigation area for **Calls**, **Contacts**, and **Agents**. **Calls** opens a full-width list with **Recent calls** and **Voicemail** tabs; no call opens automatically. Select a row to open its full-width transcript with **Calls / phone number / date** breadcrumbs. **Calls** returns to the unfiltered recent list; the phone number filters the collection to that caller, and the date is a noninteractive label. Caller filters remain active across the Recent calls and Voicemail tabs and polling updates. Browser Back/Forward restores views, and each view has a URL fragment that can be reopened or shared. The global search field and bottom player stay in place across views. Playing audio continues when returning to the list, changing its tab, or opening Contacts or Agents; explicitly opening a different call changes the player without starting playback. Polling preserves the current view, and new live calls offer **Open live call** instead of replacing the call under review. **Search** remains a disabled global placeholder; the Contacts page has its own working search.

The college logo and italic **The Hacking Banyons** link at the bottom of the sidebar switch to the team résumé view inside the dashboard without reloading or interrupting playback. The audio bar’s top-right close button pauses playback and hides the bar; **Show player** in the transcript restores it. The public `/team` URL redirects to `/dashboard#team`. It lists James Liu, Gerry Jones, Muhammed Altindal, and Shane McCarthy in that order. Their original PDFs are versioned in [`public/resumes`](public/resumes) and served as downloads; the edited SVG and supplied original are in [`public/branding`](public/branding). Photos and social links are not included.

The top-right **+** menu opens **New contact** or **New agent**. Contacts have a searchable list, profiles, call metrics, and conversation history. Four labeled fictional contacts and four demo conversations dated July–September 2026 are included; demo transcripts have no audio. Create a contact with first name, last name, and phone number, then add optional email, address, or website fields. New contacts and agent drafts save in this browser’s local storage, not on the server or across devices; changing the site origin also changes the browser storage. The bell and gear show notification and setup placeholders. Agent drafts are not connected to a provider and do not place calls or activate keypad routing.

Transcript JSON is saved with private filesystem permissions at `TRANSCRIPT_STORAGE_DIR/<CallSid>.json`; captures and transcripts stay outside Git and have no automatic deletion. Transcription supports two active calls at once, with two provider streams per call. Additional calls continue normally and show a visible transcription capacity failure. The dashboard retains ten recent finished sessions in memory/reloads; those history limits do not delete older disk files.

**Unanswered calls:** optional [voicemail](docs/VOICEMAIL.md) plays a team greeting and records a message only when the teammate has not connected. An established two-human conversation keeps its existing hangup behavior. The placeholder uses Twilio `Say`/`Record`. When configured, its finalized transcript can receive a Gemini summary after the call; ElevenLabs and detection remain unimplemented. Real-phone voicemail acceptance remains pending.

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

**Use Cloudflare instead of ngrok:** set `TUNNEL_PROVIDER=cloudflare` in the active environment and install `cloudflared`. The app still runs on this Mac at port `8000`; Cloudflare forwards public HTTPS and WebSocket traffic to it. The [Cloudflare guide](docs/CLOUDFLARE.md) covers the accountless Quick Tunnel, migration of the installed service, and a later stable domain. Quick Tunnel URLs change when the connector restarts. The supervisor updates the public URL and webhook destinations; GitHub polling continues every 30 seconds. This does not move the Python server into Cloudflare hosting.

**Cloudflare verification (2026-09-26):** the managed Quick Tunnel serves the public dashboard, health, and transcript API. Twilio and GitHub callback destinations match by API read-back. Signed HTTP callbacks and an unknown-session WebSocket rejection reach the app through Cloudflare. This network requires the HTTP/2 transport setting; a real phone call through the new tunnel remains pending.

## Start locally

Allow about 5 minutes with Python, a tunnel client, and Twilio credentials ready.

These commands are for manual development. Use [server mode](docs/SERVER.md) for automatic deployment; both modes use port `8000`, so run one mode at a time.

1. Create the Python environment and install dependencies:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements-lock.txt
   ```

   `requirements-lock.txt` pins the verified environment, including tests. `requirements.txt` and `requirements-dev.txt` list the direct dependencies for intentional upgrades. Use Python 3.11 or newer.

2. Fill in `.env` using `.env.example` as the reference. Enable capture/transcription and set private provider/storage settings using [Build 3 configuration](docs/BUILD_3.md#configure-the-installed-server) when you want live text; both optional features can remain disabled for a basic bridge test. Set `CALLEE_NUMBER` to the teammate’s full E.164 number, different from `TWILIO_NUMBER`. Preserve existing credentials; `TWILIO_AUTH_TOKEN` validates callbacks. For automatic deployment, also set a random `DEPLOY_CONTROL_TOKEN` of at least 32 characters. For Cloudflare, install with `brew install cloudflared` and set `TUNNEL_PROVIDER=cloudflare`; otherwise the default `ngrok` provider requires an installed, authenticated ngrok client.

3. Start the app and tunnel:

   ```sh
   python3 scripts/dev.py start
   ```

   The helper starts `main:app` on port `8000`, starts the selected tunnel, and saves its origin as `PUBLIC_BASE_URL` in `.env`.

4. Save the running tunnel’s webhook on the Twilio number:

   ```sh
   .venv/bin/python scripts/configure_twilio.py --apply
   ```

   This sets the number specified by `TWILIO_NUMBER` to `PUBLIC_BASE_URL/voice` for incoming calls and `PUBLIC_BASE_URL/status` for caller termination, both with method **POST**, and verifies both by read-back. Run without `--apply` to inspect settings without changing them. In manual development, repeat after a restart if the tunnel URL changes. The incoming voice URL is under the number’s **Voice → Handling for incoming calls**; the helper also configures the caller-status callback needed for hangup during the voicemail greeting.

5. Call the Twilio number from **a phone other than `CALLEE_NUMBER`**. Answer the teammate phone and exchange distinct phrases for 30 seconds. **Pass:** both people hear each other and either hangup ends both legs. Calls from the forwarding phone are rejected to prevent calling it back into itself. See the guide for no-answer and hangup tests.

Keep this computer awake and online while testing. The local app and selected tunnel must both remain running.

## Useful commands

| Action | Command |
| --- | --- |
| Open the public audio/transcript viewer | `.venv/bin/python scripts/open_dashboard.py` |
| Check processes and current webhook URL | `python3 scripts/dev.py status` |
| Stop managed app and tunnel | `python3 scripts/dev.py stop` |
| Run automated checks | `.venv/bin/python -m pytest` |
| Check local service health | `curl http://127.0.0.1:8000/health` |
| Show the voice-layer configuration | `.venv/bin/python scripts/voice_check.py --env-file .env` |
| List account voices (free key check) | `.venv/bin/python scripts/voice_check.py --env-file .env --list-voices` |
| Ask Gemini one question (no call) | `.venv/bin/python scripts/voice_check.py --env-file .env --ask "What are your hours?"` |
| Render one phrase and listen to it | `.venv/bin/python scripts/voice_check.py --env-file .env --say "Hello." --play` |
| Enroll the owner's voice once | `.venv/bin/python scripts/clone_voice.py --env-file .env --name owner <samples...>` |

An unsigned request to `/voice` is rejected. Browser visits and ordinary `curl` requests cannot stand in for a signed Twilio webhook. Validation uses the account Auth Token and the exact public URL; an API key secret is not a substitute. See [Twilio request validation](https://www.twilio.com/docs/usage/security).

Private process state and logs live in `.runtime/`. The configuration helper saves the prior Twilio settings there before applying a change. If startup fails, inspect `.runtime/app.log` and the selected tunnel's log: `.runtime/cloudflared.log` or `.runtime/ngrok.log`.

## Build 3 boundary

This build provides the two-human conference, one fixed outgoing destination, optional unanswered-call voicemail, signed callbacks/media WebSockets, private local audio capture, optional live Deepgram transcription, a public HTML viewer with finalized local WAV playback/downloads, transcript exports, persistent caller details and automatic/manual summaries, offline PCM replay, timeout cleanup, and deployment draining for outstanding work. It does not clone voices, detect deepfakes or conversational bots, generate replies, or implement an agent. Session state is in memory; explicit stop or a process crash ends continuity. The [server guide](docs/SERVER.md) covers operation and recovery.

The initial roadmap comes from [the shared Grok conversation](https://grok.com/share/bGVnYWN5_618ab7b3-9c27-4570-9709-edd7bee0bc21). The conference baseline is in [docs/BUILD_1.md](docs/BUILD_1.md); audio-capture checks are in [docs/BUILD_2.md](docs/BUILD_2.md), and current transcript checks are in [docs/BUILD_3.md](docs/BUILD_3.md). The expanded product direction is in [docs/FINAL_BUILD.md](docs/FINAL_BUILD.md).
