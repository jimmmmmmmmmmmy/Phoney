```sh
python3 scripts/server.py status
```

# Passive Operator

**Build 1:** call the configured Twilio number from a different phone and hear **“New College Data Science Team”** while the switchboard rings the fixed teammate number in `CALLEE_NUMBER`. When the teammate answers, both people talk through a Twilio conference. FastAPI and ngrok run on this Mac. Read [the Build 1 guide](docs/BUILD_1.md) for setup and the live phone checks. With no teammate configured, the original greeting still plays and the call ends.

**Verification (2026-09-26):** 137 automated tests pass, including callback races, no-answer cleanup, destination restrictions, and deployment draining. The owner confirmed live two-way audio, both hangup directions, and no-answer cleanup. Twilio records also showed both legs of the first test ending together. Automated tests use fake calls; the phone checks were performed separately.

**Final-build vision:** call someone through the operator, press `#1`, and let an agent using a clone of your own voice take over. `#2`, `#3`, and `#4` switch its saved prompts while the call continues. This is a modern version of being on hold: your AI representative keeps the conversation going while you step away. Outbound calls, inbound calls, voice enrollment, and returning control to the human are specified in [the final-build plan](docs/FINAL_BUILD.md). These features are planned, not yet implemented.

## Final build: your AI takes the call

Imagine calling a car dealership. You start the conversation, explain which car you want, and press `#1` when you want to step away. An agent that sounds like you continues the same call with the context already discussed. Instead of elevator music, the other party has your AI representative to talk to.

The phone keypad becomes a prompt selector. Implement these defaults, configurable before the call:

| Command | Agent instructions |
| --- | --- |
| `#1` | Continue this conversation for me using its existing context. |
| `#2` | Handle the wait and notify me when I am needed. |
| `#3` | Complete my saved enquiry, such as asking the dealership for an itemized quote. |
| `#4` | Switch to another prompt I configured before the call. |

Changing a prompt can route the conversation to a different agent/model while preserving your cloned voice. `#0` returns the speaking role to you and interrupts the agent. The final product supports both inbound and outbound calls routed through the operator; manual takeover works whether the other party is human or AI.

### Build it this way

Use **Python/FastAPI + two Twilio bidirectional Media Streams + Deepgram + Claude + ElevenLabs**. The Python bridge forwards the humans' audio until a keypad command substitutes the voice agent. For outbound calls, the server calls your phone first, you accept, and it calls the dealership. Both legs stay under the operator's control.

1. **Bridge the phones.** Add session state, owner acceptance, signed media WebSockets, and two-way audio forwarding.
2. **Prove takeover with a fixed clip.** Enroll your voice; make `#1` play a cloned phrase and `#0` interrupt it. Map the other keys to distinct profiles.
3. **Connect the agent.** Stream transcription → context and selected prompt → text model → cloned speech, with interruption and return-to-human handling.
4. **Handle phone menus and inbound calls.** Send IVR digits by updating only the remote call to play digits and reconnect its audio stream. Reuse the controller for incoming callers.
5. **Keep it running.** Add owner alerts, summaries, call cleanup, and deployment draining so a push waits for active calls to finish.

[**Implementation recipe →**](docs/IMPLEMENTATION.md) has exact modules, API routes, TwiML/Python examples, keypad parsing, audio mixing, timing policies, failure recovery, and a phone test for each stage. [**Voice provider adapters →**](docs/VOICE_STACK.md) has the actual cloning, STT, text-generation, and speech-streaming requests. [**Product behavior →**](docs/FINAL_BUILD.md) describes the dealership experience and acceptance criteria.

The design includes workarounds for the platform gaps: Python supplies the audio switch, Twilio call updates supply phone-menu digits, and FFmpeg supplies format conversion when needed. **These are later-build instructions; Build 1 implements the two-human bridge.** Start implementation with `operator_service/sessions.py` and the owner-only callback described in the recipe.

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

2. Fill in `.env` using `.env.example` as the reference. Set `CALLEE_NUMBER` to the teammate’s full E.164 number, different from `TWILIO_NUMBER`. Preserve existing credentials; `TWILIO_AUTH_TOKEN` validates callbacks. For automatic deployment, also set a random `DEPLOY_CONTROL_TOKEN` of at least 32 characters. Install ngrok and authenticate it if needed.

3. Start the app and tunnel:

   ```sh
   python3 scripts/dev.py start
   ```

   The helper starts `main:app` on port `8000`, starts ngrok, and saves the tunnel origin as `PUBLIC_BASE_URL` in `.env`.

4. Save the running tunnel’s webhook on the Twilio number:

   ```sh
   .venv/bin/python scripts/configure_twilio.py --apply
   ```

   This updates the number specified by `TWILIO_NUMBER` to `PUBLIC_BASE_URL/voice` with method **POST** and verifies it. Run without `--apply` to inspect settings without changing them. Repeat after a restart if the ngrok URL changes. The same fields are in the Twilio console under the number’s **Voice → Handling for incoming calls**.

5. Call the Twilio number from **a phone other than `CALLEE_NUMBER`**. Answer the teammate phone and exchange distinct phrases for 30 seconds. **Pass:** both people hear each other and either hangup ends both legs. Calls from the forwarding phone are rejected to prevent calling it back into itself. See the guide for no-answer and hangup tests.

Keep this computer awake while testing. The local app and ngrok must both remain running.

## Useful commands

| Action | Command |
| --- | --- |
| Check processes and current webhook URL | `python3 scripts/dev.py status` |
| Stop managed app and tunnel | `python3 scripts/dev.py stop` |
| Run automated checks | `.venv/bin/python -m pytest` |
| Check local service health | `curl http://127.0.0.1:8000/health` |

An unsigned request to `/voice` is rejected. Browser visits and ordinary `curl` requests cannot stand in for a signed Twilio webhook. Validation uses the account Auth Token and the exact public URL; an API key secret is not a substitute. See [Twilio request validation](https://www.twilio.com/docs/usage/security).

Private process state and logs live in `.runtime/`. The configuration helper saves the prior Twilio settings there before applying a change. If startup fails, inspect `.runtime/app.log` or `.runtime/ngrok.log`.

## Build 1 boundary

This build provides the inbound greeting, two-human conference, one fixed outgoing destination, signed callbacks, setup timeout and cleanup, health checks, and automatic deployment that waits for active calls. It does not record audio, stream media, clone voices, detect bots, or run an AI agent. Session state is in memory; explicit stop or a process crash ends continuity. The [server guide](docs/SERVER.md) covers operation and recovery.

The initial roadmap comes from [the shared Grok conversation](https://grok.com/share/bGVnYWN5_618ab7b3-9c27-4570-9709-edd7bee0bc21). The implemented switchboard and phone acceptance checks are in [docs/BUILD_1.md](docs/BUILD_1.md). The expanded product direction is in [docs/FINAL_BUILD.md](docs/FINAL_BUILD.md).
