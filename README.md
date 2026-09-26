```sh
python3 scripts/server.py status
```

# Passive Operator

**Build 0:** call the configured Twilio number and hear **“New College Data Science Team”**. The call then ends. FastAPI serves the voice webhook through ngrok. Build 1 is a separate implementation: connect two humans in a conference, with no AI. Follow [the Build 1 plan](docs/BUILD_1.md) when Build 0 passes the phone test.

**Final-build vision:** call someone through the operator, press `#1`, and let an agent using a clone of your own voice take over. `#2`, `#3`, and `#4` switch its saved prompts while the call continues. This is a modern version of being on hold: your AI representative keeps the conversation going while you step away. Outbound calls, inbound calls, voice enrollment, and returning control to the human are specified in [the final-build plan](docs/FINAL_BUILD.md). These features are planned, not yet implemented.

## Final build: your AI takes the call

Imagine calling a car dealership. You start the conversation, explain which car you want, and press `#1` when you want to step away. An agent that sounds like you continues the same call with the context already discussed. Instead of elevator music, the other party has your AI representative to talk to.

The phone keypad becomes a prompt selector. These are proposed, configurable assignments:

| Command | Agent instructions |
| --- | --- |
| `#1` | Continue this conversation for me using its existing context. |
| `#2` | Handle the wait and notify me when I am needed. |
| `#3` | Complete my saved enquiry, such as asking the dealership for an itemized quote. |
| `#4` | Switch to another prompt I configured before the call. |

Changing a prompt can route the conversation to a different agent/model while preserving your cloned voice. A proposed `#0` shortcut returns the speaking role to you and interrupts the agent. The final product supports both inbound and outbound calls routed through the operator; manual takeover works whether the other party is human or AI.

Read [the final-build specification](docs/FINAL_BUILD.md) for the voice profile, handoff context, keypad routing prototype, and acceptance checks. **The current deployed build still only plays the team greeting.**

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

2. Fill in `.env` using `.env.example` as the reference. Preserve existing credentials. `TWILIO_AUTH_TOKEN` is required to validate Twilio requests. Install ngrok and authenticate it to your ngrok account if needed.

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

5. Call the Twilio number from a phone. **Pass:** you hear “New College Data Science Team” and the call ends. A passing HTTP test alone does not verify the telephone call.

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

## Build 0 boundary

This build provides the inbound greeting, request validation, local health check, automated tests, and a repeatable app/tunnel runner. It does not dial a teammate, record calls, stream audio, detect bots, or run an AI agent.

The initial roadmap comes from [the shared Grok conversation](https://grok.com/share/bGVnYWN5_618ab7b3-9c27-4570-9709-edd7bee0bc21). Implementation decisions and acceptance checks for the next milestone are in [docs/BUILD_1.md](docs/BUILD_1.md). The expanded product direction is in [docs/FINAL_BUILD.md](docs/FINAL_BUILD.md).
