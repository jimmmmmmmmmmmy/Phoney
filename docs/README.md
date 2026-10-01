# Pick your implementation guide

**GPT migration and ChatGPT plugin:** read the [research report](CHATGPT_PLUGIN_RESEARCH.md), then follow the [five-phase build plan](CHATGPT_PLUGIN_BUILD_PLAN.md). These are proposals checked October 1, 2026; no model or production configuration has been changed by this documentation work.

Open [BUILD_3.md](BUILD_3.md) to use the live transcript dashboard. Partners can start with [DEEPFAKE_DETECTION.md](DEEPFAKE_DETECTION.md) for detection or [VOICE_STACK.md](VOICE_STACK.md) for Gemini + ElevenLabs.

**Historical Build 1–3 guides:** these describe earlier milestones, including public archive access. The current live workspace access gate is enabled, and dashboard APIs, transcripts and recordings require authorization. See [workspace privacy](WORKSPACE_STORAGE.md#shared-pin-and-remembered-phones) and the [current architecture baseline](CHATGPT_PLUGIN_RESEARCH.md#what-is-running-today) before using an older guide's deployment or security assumptions.

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
