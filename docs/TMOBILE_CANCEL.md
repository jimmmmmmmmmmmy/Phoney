# Cancel two T-Mobile lines from the Mac

The reusable mission is `tmobile-cancel`. From the repo:

```sh
python -m phoney_missions check tmobile-cancel
python scripts/mission_run.py prep tmobile-cancel
```

Artificial run, no Twilio dial:

```sh
pytest tests/test_cancel_call.py::test_artificial_cancel_and_retention_calls -q
python scripts/mission_run.py run tmobile-cancel --simulate
```

`check`, `prep`, `research`, and `report` do not dial. `place-call` still
requires `PHONEY_ALLOW_LIVE_CALL=1`, a loopback Phoney, and refuses `CI`.
Set `PHONEY_UNATTENDED=1` as well only when the owner phone should not ring.
See [phone missions](PHONE_MISSIONS.md) and the
[post-#1 audit](ARCHITECTURE_AUDIT_PHONE_MISSIONS.md). Public tactics live in
[docs/missions/tmobile-cancel](missions/tmobile-cancel/RESEARCH.md).

Phoney places this call with Twilio. Deepgram still transcribes. When
`VOICE_TTS_PROVIDER=phonon` and `AGENT_LLM_PROVIDER=nemotron`, speech comes from
local Phonon 40m and replies come from local Nemotron. ElevenLabs and Gemini
remain the fallback when those providers are off or the local server does not
answer. Do not switch the phone trunk to Telnyx.

Do not run the live call from CI or a cloud agent. Those environments do not
have the Twilio credentials, the Mac models, or the owner's phone. The command
below refuses to dial unless it is pointed at loopback and
`PHONEY_ALLOW_LIVE_CALL=1` is set on that Mac.

## What the agent is allowed to do

Destination: T-Mobile customer service `+18009378997` (1-800-937-8997).
`611` only works from a T-Mobile handset. Twilio cannot dial it.

The checked-in brief (`voice_stack/cancel_agent.py`) is:

- Account number `252027408` (override with `CANCEL_ACCOUNT_NUMBER`)
- Cancel only the Apple Watch SE line `773-988-2659` and the BYOD mobile `312-545-2413`
- Do not cancel any other line, including Beata's
- Confirm each cancellation and repeat the confirmation number and effective date
- If a PIN, passcode, SSN, or last four is required and it is not in the private env, say the account holder is needed and hang up. Do not invent one
- Decline retention offers. Ask for a supervisor only if cancellation is refused
- After both lines are resolved, or one cannot be cancelled, summarize and hang up

Outbound goal that arms this agent: `cancel-tmobile`.

## Configure the Mac

Use the private env the server already loads (repo `.env` or
`~/Library/Application Support/NewCollegeOperator/.env`). Restart Phoney after
edits. Never commit that file.

```dotenv
ALLOWED_DESTINATIONS=+18009378997
OWNER_NUMBER=+1XXXXXXXXXX
TWILIO_NUMBER=+1XXXXXXXXXX
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
OPERATOR_ADMIN_TOKEN=
PUBLIC_BASE_URL=
VOICE_AGENT_ENABLED=true
VOICE_OUTPUT_DIR=/absolute/path/to/voice-output
TRANSCRIPTION_ENABLED=true
DEEPGRAM_API_KEY=
DEEPGRAM_MODEL=nova-3
NATIVE_CONFERENCE_ENABLED=false

VOICE_TTS_PROVIDER=phonon
PHONON_BASE_URL=http://127.0.0.1:8091
PHONON_MODEL=phonon-40m
PHONON_VOICE=phonon-40m
PHONON_MODEL_PATH=/Users/james/.voice-assistant/models/phonon-40m

AGENT_LLM_PROVIDER=nemotron
NEMOTRON_BASE_URL=http://127.0.0.1:8088
NEMOTRON_MODEL=nemotron-en
NEMOTRON_MODEL_PATH=/Users/james/.voice-assistant/models/nemotron-en

CANCEL_ACCOUNT_NUMBER=252027408
CANCEL_LINES=7739882659:Apple Watch SE,3125452413:BYOD mobile
CANCEL_ACCOUNT_PIN=
CANCEL_SSN_LAST4=
```

`ALLOWED_DESTINATIONS` stays an allowlist. This adds T-Mobile customer service.
It does not allow every number. `OWNER_NUMBER` and `TWILIO_NUMBER` still cannot
be destinations. The cancel agent starts on its own after you press 1. Saved
keypad agents are separate; turn on `AGENT_MANAGEMENT_ENABLED` and
`WORKSPACE_STORAGE_DIR` only if you also want the Agents tab.

Keep the cloud keys if you want a fallback when a local server is down:

```dotenv
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.5-flash-lite
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
ELEVENLABS_MODEL=eleven_v4_turbo
```

With both filled in, Phonon and Nemotron are used first. If `GET /health` on
that base URL fails, the call uses Gemini and ElevenLabs. Set
`VOICE_TTS_PROVIDER=elevenlabs` and `AGENT_LLM_PROVIDER=gemini` to force the
cloud path.

## Start Phonon and Nemotron

Phoney does not spawn the Rust voice-assistant. Start the servers you already
run for `~/.voice-assistant/models/`, then point the base URLs at them. The
model path settings are checked by `--check` so you can see whether this Mac
has the weights; the live call talks HTTP, not to the files directly.

Phonon must accept:

```http
POST /v1/audio/speech
Content-Type: application/json

{"model":"phonon-40m","voice":"phonon-40m","input":"Hello.","response_format":"ulaw_8000"}
```

The body should be raw 8 kHz mono mu-law (`audio/basic`). WAV PCM is also
accepted and converted. `GET /health` should answer with anything below HTTP 500.

Nemotron must accept OpenAI-style streaming chat:

```http
POST /v1/chat/completions
Content-Type: application/json

{"model":"nemotron-en","messages":[{"role":"system","content":"..."},{"role":"user","content":"..."}],"stream":true}
```

Stream `data: {"choices":[{"delta":{"content":"..."}}]}` and finish with
`data: [DONE]`. A single JSON body is also accepted. Optional
`NEMOTRON_API_KEY` is sent as a bearer token when the server requires one.

Use the ports your existing processes already bind. `8091` and `8088` above
are examples.

## IVR digits

Twilio media streams cannot send DTMF. Phoney updates the **existing** remote
call with `<Play digits="..."/>` and reconnects that leg's stream. The Call SID
does not change. A failed update is not retried, because the first request may
have played. Digit values are not written to logs; the count is.

The owner keypad still queues digits in human mode (`##` sends `#`). The cancel
agent can emit a line the caller never hears:

```text
[/DTMF 0]
```

`w` is a half-second pause and `W` is a one-second pause. At most 32 characters,
and at most 16 digit updates on one call.

`voice_stack/ivr.py` holds the default playbook. It auto-sends only when the
recording just said "press N" for a representative. Account-change and cancel
choices are left to the model so a changed menu cannot cancel the whole
account. Menus change. The model is told to recover from a mismatch instead of
guessing a number.

Override the playbook with a JSON file:

```json
{
  "entries": [
    {
      "id": "representative",
      "phrases": ["representative", "customer care"],
      "auto": true,
      "priority": 0
    },
    {
      "id": "known-cancel-menu",
      "phrases": ["to cancel one line press 4"],
      "digits": "4",
      "auto": true,
      "priority": 0
    }
  ]
}
```

Set `TMOBILE_IVR_PLAYBOOK` to that file's absolute path. An unreadable file
keeps the default playbook.

Manual digits, if you need to steer a stuck menu:

```sh
curl -s -X POST "http://127.0.0.1:8000/api/sessions/$SESSION/dtmf" \
  -H "Authorization: Bearer $OPERATOR_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"digits":"0"}'
```

## Place the call

From the repo on the Mac, with Phoney already listening on port 8000:

```sh
python scripts/cancel_tmobile.py --check
```

That prints the destination, whether `+18009378997` is allowlisted, which
providers are selected, and whether the model paths exist. It does not dial.

When the check looks right and the local health URLs answer:

```sh
PHONEY_ALLOW_LIVE_CALL=1 python scripts/cancel_tmobile.py --place-call
```

What happens:

1. Your `OWNER_NUMBER` rings. Press 1 to connect. Until you do, T-Mobile is not dialed.
2. Twilio dials `+18009378997` from `TWILIO_NUMBER`.
3. The cancel agent listens. On a "press 0 for a representative" menu it sends that digit. Other menus are answered with `[/DTMF ...]` or a short spoken reply from Phonon.
4. With a person, it cancels only the two named lines, reads back confirmation numbers and effective dates, summarizes, and hangs up.

Success sounds like a short spoken summary naming both lines, each confirmation
number, and each effective date, then the call ends. The session transcript
(`GET /api/sessions/<id>` with the admin token) should show those numbers in
the agent's turns. A refusal that asks for a PIN you did not configure should
end with a request for the account holder and no invented digits.

The script posts only to a loopback Phoney (`http://127.0.0.1:8000` by default).
It will not call Twilio itself, and it exits if `CI` or `GITHUB_ACTIONS` is set.
