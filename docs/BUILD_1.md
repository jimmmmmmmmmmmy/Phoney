**Next: set `CALLEE_NUMBER` in `.env` to the consenting teammate’s E.164 phone number, then implement the switchboard below.**

# Build 1 — two humans talking

**Status: planned, not implemented by Build 0.** Budget 2–4 hours for implementation and two-phone verification after the inbound greeting works. The estimate assumes outbound calling is enabled for the teammate’s destination.

Success is one incoming call to the Twilio number, one outbound call to a fixed teammate, and intelligible two-way conversation. There is no AI, detector, transcription, recording, media WebSocket, or Redis in this milestone.

## Call flow

```mermaid
sequenceDiagram
    participant Caller as Caller phone
    participant Twilio
    participant App as FastAPI
    participant Callee as Teammate phone
    Caller->>Twilio: Call the Twilio number
    Twilio->>App: POST /voice (CallSid)
    App-->>Twilio: Dial Conference operator-{CallSid}
    Twilio->>App: Conference participant-join (caller)
    App->>Twilio: Create outbound conference participant
    Twilio->>Callee: Ring the fixed CALLEE_NUMBER
    Callee->>Twilio: Answer
    Twilio->>App: Call status + conference participant-join
    Caller->>Callee: Two-way audio through conference
```

The caller joins first and waits. Only the signed caller-join callback may initiate the outbound participant, after the actual `ConferenceSid` is known. This avoids dialing a teammate before the caller reaches the room. Conference creation and participant management are supported by [Twilio’s Conferences API](https://www.twilio.com/docs/voice/api/conference-resource).

## Implementation order

1. **Configuration and state.** Add the fields below, validate startup configuration, and create a single-process session store keyed by the inbound `CallSid`.
2. **Inbound conference.** Replace the greeting TwiML in `POST /voice` with the caller’s conference instructions; attach conference callbacks and a dial-completion action.
3. **Outbound participant.** Add the caller-join handler and the outbound Twilio API call. Protect creation against duplicate callbacks.
4. **Lifecycle and failures.** Track joins, leaves, ringing, terminal call results, abandoned callers, and timeouts. Cancel orphaned calls and end the room when appropriate.
5. **Verification.** Run automated checks, restart the local runner, and complete the two-phone acceptance checks below.

## Files and interfaces

These are proposed additions; Build 0 does not already contain them.

| File | Responsibility |
| --- | --- |
| `app.py` / `config.py` | Mount routes in `create_app`, extend `Settings`, and preserve health/signature behavior. `main.py` remains the entrypoint. |
| `switchboard/models.py` | `CallSession`: inbound SID, conference name/SID, outbound SID, labels, phase, timestamps, outbound-attempt state, last event sequence. |
| `switchboard/service.py` | In-memory session dictionary, lock-protected transitions, Twilio client calls, timeout/cleanup tasks. |
| `switchboard/routes.py` | Signed form callbacks and TwiML rendering; no arbitrary destination supplied by request. |
| `tests/test_switchboard.py` | Mocked Twilio API, signed webhook requests, duplicate/racing events, failure and cleanup behavior. |

Run one Uvicorn worker without reload during calls. A dictionary plus `asyncio.Lock` is enough for this local milestone. State disappears on restart, so stop active calls before restarting. Multi-process operation and crash recovery require a shared persistent store in a later milestone.

| Setting | Build 1 use |
| --- | --- |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Account identity and mandatory verification of every Twilio webhook. |
| `TWILIO_API_KEY` / `TWILIO_API_SECRET` | Optional REST credentials, supplied together; otherwise use account SID/Auth Token. Match the names already used in local configuration. |
| `TWILIO_NUMBER` | The owned, voice-capable Twilio number used as outbound `from`, in E.164 format. |
| `CALLEE_NUMBER` | One explicitly allowlisted teammate number, in E.164 format; reject the Twilio number itself. |
| `PUBLIC_BASE_URL` | Current ngrok HTTPS origin for TwiML and callbacks; already managed by the development runner. |

Do not accept `to`, a dial destination, or an arbitrary callback URL from inbound form data. Keep credentials and phone numbers out of committed fixtures and ordinary logs. REST API key authentication does not replace the account Auth Token used for signature verification. Use the official validator with every received form parameter and the exact public URL, including query parameters. [Twilio security](https://www.twilio.com/docs/usage/security) and [API authentication](https://www.twilio.com/docs/usage/requests-to-twilio).

On a Twilio trial account, verify the teammate's destination under **Verified Caller IDs** before testing outbound calls. Check that the destination country is enabled in Voice geographic permissions.

## Route contract

| Route | Required behavior |
| --- | --- |
| `POST /voice` | Validate signature and account; create/reuse session for inbound `CallSid`; return caller conference TwiML. |
| `POST /conference/events/{parent_call_sid}` | Validate signature; bind/verify conference identity; process `join`, `leave`, `start`, `end`; initiate exactly one outbound attempt on the caller’s first join. |
| `POST /calls/status/{parent_call_sid}` | Validate signature; verify the outbound SID; track initiated/ringing/answered/completed events and terminal `CallStatus`. |
| `POST /conference/finished/{parent_call_sid}` | Validate signature; finish the caller leg with a short result or hangup after `<Dial>` returns; repeat safely. |
| `GET /health` | Preserve the existing service health endpoint; exclude secrets and call data. |

The current `app.py` nests `validate_twilio` inside `create_app` and requires `CallSid` on every request. Extract its signature/account validation into a reusable dependency, then validate callback-specific fields separately: conference-wide start/end events need not include `CallSid`. Keep the existing `/status` behavior until its callers are migrated or remove it if it remains unused.

Return TwiML only where Twilio expects call instructions. Event receivers acknowledge accepted callbacks promptly with a successful HTTP response; they do not drive call audio through their response body. Run blocking Twilio SDK calls outside the async event loop and use bounded API timeouts.

## Conference choices

Use `operator-{inbound CallSid}` as the conference name and stable labels `caller` and `callee`. The caller has `startConferenceOnEnter=false`; the callee has `startConferenceOnEnter=true`. Set `endConferenceOnExit=true` for both in Build 1, `beep=false`, and `maxParticipants=2`. Configure callback URL/events on the caller, who joins first. Twilio uses the first participant’s conference callback configuration. Labels must be unique within the room; leaving with `endConferenceOnExit=true` ends it for everyone. [Twilio Conference reference](https://www.twilio.com/docs/voice/twiml/conference).

After the caller’s join event, create the outbound participant using `client.conferences(conference_sid).participants.create(...)`: `from_=TWILIO_NUMBER`, `to=CALLEE_NUMBER`, `label="callee"`, matching start/end flags, a 25-second ringing timeout, and the call-status URL. Subscribe to `initiated`, `ringing`, `answered`, and `completed`. Participant creation initiates the outbound call; no separate callee TwiML route is needed for this design. Twilio adds a small timeout buffer, so the configured timeout is not an exact stopwatch deadline. [Conference Participants API](https://www.twilio.com/docs/voice/api/conference-participant-resource).

An `answered` event alone is not proof that both people can talk. Mark the session connected only when both labeled participants have joined and the conference has started. A voicemail system can answer; the live test must establish that the teammate actually answered. Call-progress event subscriptions differ from the `CallStatus` values carried in those events. Handle `busy`, `no-answer`, `failed`, `canceled`, and `completed` as terminal outcomes. [Twilio Call resource](https://www.twilio.com/docs/voice/api/call-resource).

## Duplicate events and cleanup

1. **Reserve the dial attempt under a lock.** Change `waiting` to `dialing` before the API request, then release the lock for network I/O. A duplicate `/voice` or join callback must never trigger another outbound request. Store callback event identity/sequence so late events cannot reopen a terminal session.
2. **Handle uncertain API outcomes conservatively.** If participant creation times out, reconcile using the known conference and `callee` label before retrying. Do not blindly repeat an outbound call whose success is unknown. For Build 1, a failed attempt ends the session; a fresh inbound call is the manual retry.
3. **Handle the caller leaving during dialing.** Mark the session ending and cancel any queued/ringing outbound call. Recheck session state after participant creation returns; if the caller already left, cancel that newly returned call too.
4. **Bound the waiting period.** On busy, no-answer, failure, or a local deadline (for example, 45 seconds from dial reservation), stop the outbound leg and redirect the caller to a short unavailable message followed by hangup, or end the conference. Make cleanup safe to repeat.
5. **Retain terminal session tombstones briefly.** Keep completed sessions for at least the expected callback retry window before removing them, with a bounded store size. This prevents a delayed webhook from recreating a completed call. Log IDs/state transitions, not raw callback bodies.

## Acceptance checks

Automated checks use mock phone numbers and a mocked Twilio client; they do not place paid calls.

1. **Happy path:** one signed inbound request and caller join create one conference session and one outbound participant with the configured destination. A different inbound SID gets a different room.
2. **Request security:** unsigned/invalid signatures and wrong account callbacks fail before a state change or REST request. Public HTTPS signature reconstruction still works through ngrok.
3. **Lifecycle:** duplicate, delayed, and racing callbacks never duplicate dialing or reopen terminal state. Busy/no-answer/API failure/timeout release the caller. Caller hangup during creation cancels the eventual outbound leg.
4. **Real phones:** caller dials the Twilio number; teammate answers; both speak and hear distinct phrases for 30 seconds. Repeat once with each person hanging up first and verify the other leg ends.
5. **Real no-answer:** teammate does not answer; the caller receives the configured failure outcome within the deadline. Verify the Twilio logs show no orphaned outbound leg or active conference.

Record the date, observed Call/Conference SIDs, and pass/fail results in a local test note. Do not mark Build 1 complete based only on mocked tests or REST responses.

## Later milestones

The final product adds outbound calls and owner keypad shortcuts that delegate the conversation to an agent using the owner's cloned voice. See [Final build — put your AI on the call](FINAL_BUILD.md) for `#1`–`#4` prompt selection, context transfer, and return-to-human behavior. Build 1 remains the two-human switchboard; manual delegation will work independently of AI detection.

| Build | Deliverable |
| --- | --- |
| 2 | Capture clearly identified call audio with Media Streams; decode the incoming audio format correctly and write playable WAV files. |
| 3 | A manual takeover button with a proven agent-audio bridge, readiness handshake, and cleanup behavior. |
| 4 | A real conversational agent using the proven bridge. |
| 5 | A detector that can trigger the already-tested takeover flow automatically. |
| 6 | Transcript, conversation context, Pangram integration, and intent handling after the core call path is reliable. |

For Build 2, `<Start><Stream>` observes audio and continues to the next TwiML verb; it cannot send agent audio back. `<Connect><Stream>` supports bidirectional audio but blocks subsequent TwiML until it ends. Merely placing it before `<Dial><Conference>` does not add an AI participant. [Twilio Stream reference](https://www.twilio.com/docs/voice/twiml/stream).

Before Build 3, explicitly design the agent leg. One documented option adds an `app:<APP_SID>` conference participant whose TwiML application returns `<Connect><Stream>`. Prove that bridge with simple audio first, make the agent ready before removing a human, and revise `maxParticipants` and `endConferenceOnExit` so removing the callee does not terminate the caller or agent. [Twilio’s conference/Media Streams bridge guide](https://help.twilio.com/articles/45314613523867).

The milestone sequence follows [the shared Grok conversation](https://grok.com/share/bGVnYWN5_618ab7b3-9c27-4570-9709-edd7bee0bc21); the route layout, lifecycle rules, and verification plan above are implementation recommendations for this repository.
