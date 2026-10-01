# Native conference pilot

Keep `NATIVE_CONFERENCE_ENABLED=false` until the controlled phone checks below
pass. The flag defaults to false. This pilot moves **outbound phone-to-phone
calls** into Twilio's conference mixer while retaining a separate streamed AI
participant for takeover.

Incoming calls keep the existing agent-capable relay and voicemail path. Browser
microphone calls use their independent `BROWSER_VOICE_ENABLED` flag; see
[native browser calling](BROWSER_CALLING.md). With that flag off they keep the
existing relay. For
other dashboard destinations, the owner-first phone callback and press-1
acceptance remain in use. Native mode is selected when a new session is created;
changing configuration does not convert an active call.

## Configure the pilot

1. Create a Twilio **TwiML App** for the conference agent. Set its Voice Request
   URL to `https://<public-host>/twilio/native-agent`, with request method **POST**.
   Use the server's configured HTTPS public origin. Copy its `AP...` Application
   SID. This document does not create an application or change the account.
2. Add the two settings below to the installed service's private environment,
   outside the checkout and deployment release directories. The installed Mac
   service reads `~/Library/Application Support/NewCollegeOperator/.env`.
3. Keep the existing Twilio credentials, `OWNER_NUMBER`, destination allowlist,
   transcription, recording and agent configuration. Enabling this flag does not
   create provider credentials or publish an agent. No incoming-number webhook
   change is required for this outbound-only pilot.
4. Deploy and restart through the existing supervised process after active calls
   finish. Confirm the serving revision and native-mode configuration before
   making the controlled call. See [server operations](SERVER.md).
5. Run the acceptance checks below with the owner phone and one approved test
   destination. Leave the flag off for wider use until those checks pass.

```dotenv
NATIVE_CONFERENCE_ENABLED=false
TWILIO_CONFERENCE_APP_SID=APxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Set `NATIVE_CONFERENCE_ENABLED=true` only for the configured pilot. The TwiML App
must belong to the same configured Twilio account and invoke this service's
signed agent endpoint. Application SIDs identify the app; account credentials
remain server-side.

When the supervised service detects a changed public tunnel URL, its existing
`scripts/configure_twilio.py --apply` step also updates the enabled pilot's
configured TwiML App to `PUBLIC_BASE_URL/twilio/native-agent`, using POST. It
verifies the account and reads the settings back. The original App Voice URL and
method are saved once in the private `.runtime/twilio-before-native-application.json`
backup. Running the helper without `--apply` only reports configuration,
including `native_agent_matches`. The App is synchronized when either native
phone mode or native browser calling is enabled; disabling both skips the App.

## Call flow and controls

Both human phone legs join a native Twilio conference. Their conversation no
longer depends on Python forwarding every audio frame through the tunnel. Passive
streams still send the original caller input and playback to Phoney's recording,
transcription and detection pipeline. The observer streams remain mono 8 kHz
mu-law.

An AI participant is added through `To=app:<ApplicationSid>` with a session,
generation and stream token. Its TwiML uses `<Connect><Stream>` on **that separate
bot call**. The bot initially joins muted; activation controls when it becomes
audible. The original caller microphone remains the source for Deepgram and
Modulate, rather than the bot's mixed conference input. Delivered AI replies are
stored with agent attribution. AI speech and the caller's actual playback remain
available in the call recording.

Native phone controls use **`*N` to select agent N** and **`*0` to resume human
speech**. Press `*`, wait for the menu prompt, then press the desired number.
Pressing `*` temporarily leaves the owner's conference seat and opens a
one-digit menu. No entry returns to the conference
after the menu timeout. The owner briefly cannot hear the caller while in this
menu. The caller and owner phone connections stay up. Returning during AI mode
keeps the owner microphone muted while they listen to the caller and AI.

Every owner rejoin starts muted. The signed participant-join callback reconciles
the latest mode and unmutes the owner for human conversation. This prevents a
delayed rejoin response from restoring the microphone during AI speech. Until
that callback and conference control request finish, the owner can listen but
cannot speak into the conference.

The existing relay continues using **`#N` / `#0`**. Native conference mode does
not claim to receive those shortcuts through passive Media Streams. Protected
dashboard/API agent selection and return-to-human controls remain available.

Takeover preparation preserves human conversation until the disclosure starts.
The owner is then muted and can listen to the AI/caller exchange. Returning to
human control cancels agent generation and buffered playback and restores the
owner. Conference control crosses the network; return timing must be measured
and is not the relay's local instantaneous mode change.

## Quality and failure boundaries

Native bridging removes the app server and observer WebSockets from the human
audio forwarding path. It does not guarantee HD audio. PSTN endpoints, carrier
routes and codec negotiation can still constrain phone-to-phone audio. Twilio's
passive Media Streams exports and this bot's bidirectional stream remain 8 kHz
mu-law. Conference participants use the `small` jitter buffer setting; measure
quality and latency on the real route.

Losing a passive observer WebSocket should leave the native human conversation
connected and return an active AI exchange to human control. The pilot attempts
one passive-stream restart per human leg per call, using a fresh generation and
token. This uses the Call Streams API and preserves the conference TwiML.
Recording, transcript and detection still have a gap during the interruption;
recovery cannot reconstruct missing audio. Takeover remains unavailable while a
required observer is detached. A failed restart or a second disconnection needs
a new call to restore observation. The REST phone-status check separately
recovers a missed hangup.

A terminated or disconnected bot generation becomes unavailable immediately.
The pilot does not automatically create a replacement participant during that
call; start a new call before trying AI takeover again. Agent failure should
restore human speech while the native human legs are healthy. The application
still owns call control, recording/storage, provider sessions, summaries and
cleanup; native audio is not an assurance that every feature survives a process
or network failure.

## Controlled acceptance

1. **Native human audio:** start an approved outbound call from the dashboard,
   answer the owner callback and press 1. Exchange distinct phrases in both
   directions. Interrupt only the passive observer connection; verify both
   phones remain able to talk. Verify one restart restores observation with a
   documented gap. Repeat the interruption and verify no repeated restart;
   start a fresh call before testing AI takeover.
2. **Supervised takeover:** select a published ready agent with `*N` or the
   protected dashboard/API control. Verify the disclosure, owner microphone
   mute, owner hearing both caller and AI, and caller interruption of AI speech.
   Verify human audio remains live during preparation.
3. **Return and races:** use `*0` and the protected return control during an AI
   answer and during preparation. Verify queued agent speech stops, the owner
   can speak again, and delayed provider/bot events cannot reactivate the agent.
   Verify the one-digit menu timeout returns the owner to the call.
4. **Evidence and cleanup:** hang up the caller and verify owner/bot cleanup.
   Verify the saved call has its original caller ID, caller-only detection,
   attributed human/agent transcript, recording and post-call summaries. Repeat
   with owner hangup, remote no-answer and a bot/provider failure.
5. **Compatibility:** with the flag disabled, verify ordinary callback calling
   and `#N` / `#0`. With the flag enabled, verify incoming voicemail and the
   browser-microphone exception still use their existing routes.

Offline tests verify XML, signed callbacks, routing state and provider contracts.
They do not prove carrier audio quality, TwiML App permissions or real-phone
acceptance. Record the deployed revision and results from these controlled calls
before marking the pilot accepted.

Provider references: [TwiML App conference participants](https://www.twilio.com/en-us/blog/developers/tutorials/product/connect-twiml-app-twilio-conference),
[Participants API](https://www.twilio.com/docs/voice/api/conference-participant-resource),
[conference attributes](https://www.twilio.com/docs/voice/twiml/conference),
[Dial star control](https://www.twilio.com/docs/voice/twiml/dial#hanguponstar) and
[Gather timeout dispatch](https://www.twilio.com/docs/voice/twiml/gather#actiononemptyresult).
Observer recovery uses the [Call Streams API](https://www.twilio.com/docs/voice/api/stream-resource#create-a-stream).
