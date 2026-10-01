# Native browser calling

Enable `BROWSER_VOICE_ENABLED` after configuring the two TwiML Apps below. The
flag defaults to false and is independent of the outbound phone pilot's
`NATIVE_CONFERENCE_ENABLED` flag. A new browser session selects its transport;
changing configuration does not convert an active call.

## Configure the installed service

1. Use a Twilio Standard API key and secret for the configured account. Keep them
   in the installed service's private environment, never in JavaScript or the
   checkout. Twilio Restricted API keys cannot issue Voice SDK access tokens.
2. Configure a **browser TwiML App** with Voice Request URL
   `PUBLIC_BASE_URL/twilio/browser-voice`, method **POST**, and Call Status Changes
   URL `PUBLIC_BASE_URL/twilio/browser-status`, method **POST**.
3. Configure a **separate conference-agent TwiML App** with Voice Request URL
   `PUBLIC_BASE_URL/twilio/native-agent`, method **POST**. Both Apps must belong
   to the configured Twilio account. The separate URLs require distinct App SIDs.
4. Add the settings below to
   `~/Library/Application Support/NewCollegeOperator/.env`. Run
   `scripts/configure_twilio.py --apply --env-file <installed-env>` through the
   existing deployment environment, then deploy and restart after active calls
   finish. See [server operations](SERVER.md).
5. Confirm `/health` reports `browser_voice_enabled: true`, unlock the dashboard,
   and run one approved browser call through the checks below.

```dotenv
BROWSER_VOICE_ENABLED=true
TWILIO_BROWSER_APP_SID=APxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_CONFERENCE_APP_SID=APyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
TWILIO_API_KEY=SKxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_API_SECRET=<private-secret>
NATIVE_CONFERENCE_ENABLED=false
```

Keep the existing destination policy, microphone permission, transcript,
detection, recording and voice-agent configuration. This feature does not change
the incoming-number routing or enable native phone-to-phone calls.

The webhook helper checks both Apps before updates and reads back the browser
Voice URL and status callback. It also synchronizes the agent App when browser
voice is enabled while the phone pilot stays disabled. Read-only execution
reports `browser_voice_matches` and `native_agent_matches`. Initial App routing is
saved once in private `.runtime/twilio-before-browser-application.json` and
`.runtime/twilio-before-native-application.json` backups. The supervised service
also invokes the helper when a public tunnel URL changes; explicitly apply it
when enabling this feature on an unchanged public origin.

## Audio and access boundaries

The browser uses Twilio's Voice JavaScript SDK with Opus preferred and PCMU as a
fallback. Human audio travels between the browser and Twilio's conference mixer,
without the custom browser audio worklet, 8 kHz microphone conversion or Python
frame forwarding. The destination phone's carrier and negotiated codec still
limit the final audio quality; Opus on the browser leg does not guarantee HD
audio across the PSTN.

The authenticated browser-token endpoint returns an outbound-only access token
for identity `phoney_<session-id>` and the browser App. Its lifetime covers the
configured maximum call duration plus five minutes for setup. Incoming SDK calls
are not granted. A separate server-generated session nonce is included in SDK
connect parameters; it is independent of passive media-stream tokens. The signed
browser TwiML endpoint validates the identity, nonce and bound Call SID. A token
does not grant the caller an arbitrary destination.

The native caller input remains the source for Deepgram and Modulate. Passive
observer streams and the streamed AI participant remain 8 kHz mu-law; those
analysis and recording exports do not determine the browser leg's negotiated
codec. The dashboard's agent-selection and return-to-human controls remain
available. Phone `*N` menus from the phone pilot do not apply to the browser seat.

## Controlled checks

1. **Human call:** confirm the dashboard connects microphone and speaker audio
   to an approved phone destination. Check speech in both directions and confirm
   the selected SDK codec with browser diagnostics.
2. **Observation:** confirm caller and owner transcript attribution, independent
   caller detection and a playable recording after the call.
3. **Takeover:** select an agent in the dashboard. Confirm the owner can hear the
   caller and AI while the owner's microphone is muted. Return to human control
   and confirm speech resumes in both directions.
4. **Failure isolation:** interrupt a passive observer stream in a controlled
   test. Human audio should stay connected. The existing bounded observer retry
   preserves the conference; missing observations remain a visible evidence gap.
5. **Cleanup:** hang up first from the phone, then repeat from the browser. Check
   that both legs, the bot and session resources end without a stranded call.

Twilio references: [access-token grants and lifetime](https://www.twilio.com/docs/iam/access-tokens),
[Voice SDK codec preferences](https://www.twilio.com/docs/voice/sdks/javascript/twiliodevice#deviceoptions).
