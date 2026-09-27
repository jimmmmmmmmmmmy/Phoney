# Pick your implementation guide

Open [BUILD_3.md](BUILD_3.md) to use the live transcript dashboard. Partners can start with [DEEPFAKE_DETECTION.md](DEEPFAKE_DETECTION.md) for detection or [VOICE_STACK.md](VOICE_STACK.md) for Gemini + ElevenLabs.

**Implemented through Build 3:** Twilio conference, optional unanswered-call voicemail, passive WAV recording, live Deepgram transcription, public HTML/JSON/text viewing, finalized local WAV playback/downloads, and offline PCM replay. Anyone with the public ngrok URL can read/download transcript text and play/download finalized local recordings without signing in. Synthetic audio passed real Deepgram and local transport/browser checks; actual phone capture/transcription remains pending. Optional Modulate caller analysis is integrated; see its setup and duration-based alert policy below. See the root [verification status](../README.md) for test and deployment evidence.

| Work | Start here | Concrete outcome |
| --- | --- | --- |
| Deepfake detection | [Architecture, code examples, evaluation, rollout](DEEPFAKE_DETECTION.md) | PCM windows → provider results → qualified observation; offline first. |
| Modulate integration | [Exact batch and streaming contracts](MODULATE.md) | API adapters with correct authentication, formats, verdicts, and timestamps. |
| Other detector choices | [Resemble, Reality Defender, and local baseline](DETECTION_ALTERNATIVES.md) | Alternatives if account access, cost, or accuracy changes the choice. |
| Conversational voice clone | [Gemini + ElevenLabs voice stack](VOICE_STACK.md) | Transcription → Gemini dialogue/tools → authorized cloned speech. |
| Audio and text inputs for partners | [Partner handoff](PARTNER_HANDOFF.md) | Typed PCM replay, manifest schema, transcript segments and public exports. |

## Twilio and product references

| Work | Guide |
| --- | --- |
| Unanswered-call message recording and provider-free next tasks | [Voicemail](VOICEMAIL.md) |
| Current transcript/recorded-audio dashboard and acceptance | [Build 3](BUILD_3.md) |
| Contacts and agent drafts across tunnel URLs | [Workspace persistence](WORKSPACE_STORAGE.md) |
| Telephone audio quality and higher-rate options | [Audio quality / VoIP](AUDIO_QUALITY.md) |
| Recording and capture acceptance | [Build 2](BUILD_2.md) |
| Human-to-human conference | [Build 1](BUILD_1.md) |
| Keypad delegation and outbound calling | [Final product](FINAL_BUILD.md) and [Python bridge implementation](IMPLEMENTATION.md) |
| Mac server, ngrok, GitHub deployment | [Server operations](SERVER.md) |

The selected partner stack uses Google Gemini for conversation and ElevenLabs for voice generation. Those are the two relevant sponsor categories listed on the [ShellHacks prize page](https://www.mlh.com/events/shellhacks-b9/prizes); [Gemini resources](https://www.mlh.com/partners/gemini) and [ElevenLabs resources](https://www.mlh.com/partners/elevenlabs) explain account setup. A plan in a README is not a working API integration: demonstrate actual provider usage when that partner milestone is implemented.

- [Modulate integration audit and configuration](MODULATE_INTEGRATION.md) — imported collaborator work, lifecycle fixes, saved results, and dashboard display.

- [Caller AI alerts and historical WAV analysis](CALLER_AI_ALERTS.md) — caller-only audio, duration thresholds, persisted intervals, and opt-in background processing.
