# Architecture audit: post-#1 cancel path

Commit `0601b6d` added a T-Mobile cancel call on the Twilio trunk with Deepgram
STT, optional local Phonon and Nemotron, and DTMF on the existing remote Call
SID. This note is what that path did well, what was fragile, and what had to
change for unattended multi-mission use. It includes the voice-path latency
audit. No call was placed to produce it.

## What was solid

- Destination allowlist still rejects the owner number and the Twilio number.
  `+18009378997` is dialed only when it is listed. `611` is documented as not
  dialable from Twilio.
- `scripts/cancel_tmobile.py` posts only to loopback Phoney, requires
  `PHONEY_ALLOW_LIVE_CALL=1`, and refuses `CI` / `GITHUB_ACTIONS`.
- The owner press-1 gate runs before the destination is dialed. That is the
  right default for a human debugging a call.
- IVR digits update the existing remote call with `<Play digits>` and reconnect
  that leg's stream. The Call SID does not change. A definite REST failure
  rolls back. A timeout is not retried. Logs store the digit count, not the
  digits.
- The default playbook auto-sends only a "press N for a representative" digit
  the caller just heard. Account and cancel choices are model-only.
- `[/DTMF ...]` is stripped before TTS. `[/END CALL]` still hangs up after playback.
- Deepgram is the streaming STT client: mulaw 8 kHz, interim results, 750 ms
  endpointing, 1000 ms utterance end, VAD events. There is no second STT provider.
- Phonon and Nemotron are selected by `VOICE_TTS_PROVIDER` and `AGENT_LLM_PROVIDER`.
  If the local health check fails and cloud keys exist, ElevenLabs and Gemini
  are the fallback. The trunk stays Twilio.

## What was fragile

- The mission was a goal string, `cancel-tmobile`, plus a Python prompt. A
  second task meant another special case in `routes.py` and `runtime.py`.
- Retention was the sentence "Refuse retention offers." A threshold could not
  change the decision without editing prose.
- The owner press-1 gate was mandatory, so a prepared mission could not run
  after the human left.
- PIN and SSN could be copied into the stored agent snapshot prompt.
- Research did not exist. IVR hints lived only in code.
- Spoken length was "be concise," with no budget. ElevenLabs v3/v4 can join
  short sentences into one longer TTS phrase.
- Phonon waits for the entire HTTP body before yielding audio. A long reply
  delays time-to-first-audio by the whole synthesis.

## What changed for unattended multi-mission

- A versioned pack schema. T-Mobile is `phoney_missions/packs/tmobile-cancel.json`.
  New tasks copy `phoney_missions/templates/mission.json`.
- Policy fields `min_monthly_reduction_cents` and `min_months` generate the
  brief. Tests accept `$20/month for 12 months` at the default and reject a
  one-time credit and a 6-month credit.
- `brevity: strict` is required on the template and the T-Mobile pack. Sample
  replies over 1 sentence / 18 words / 110 characters fail. Confirmations may
  use 2 sentences, 32 words, 180 characters. Strict missions do not coalesce
  phrases, and each phrase is fit to that budget before TTS.
- `PHONEY_UNATTENDED=1` together with `PHONEY_ALLOW_LIVE_CALL=1` skips the owner
  ring for missions only. CI still cannot skip it. The owner leg is not dialed.
- CLI: `prep`, `research`, `check`, `place-call`, `report`, `run`.
  `python scripts/mission_run.py run tmobile-cancel --simulate` runs the
  scripted call on a fake dialer. It does not contact Twilio. The same
  scenario is `pytest tests/test_cancel_call.py::test_artificial_cancel_and_retention_calls`.
- Curated public research in `docs/missions/tmobile-cancel/`. Secrets are
  rejected if they would be written. X refresh is a TODO after 2026-10-29 00:00 UTC.
- The stored snapshot prompt is public. PIN and SSN last four are added only
  on the per-reply runtime instruction.

## STT

Deepgram is already the primary STT on main. `transcription/service.py` opens
`wss://api.deepgram.com/v1/listen` and labels stored transcripts
`"provider": "deepgram"`. No Whisper, Google, or other STT client is wired.
Missions keep that. When `DEEPGRAM_API_KEY` is set and is not the placeholder
`REPLACE_ME`, `select_stt` returns Deepgram. When it is missing, the provider
is `unconfigured`, not a substitute. James's Mac `.env` is not in this
checkout. `python -m phoney_missions check tmobile-cancel` on that Mac prints
`stt=deepgram` when the key is present, and does not print the key.
`TRANSCRIPTION_ENABLED=true` is still required for the live socket; the key
alone is the selection signal the mission check reports.

Prefer Deepgram because Twilio media is already 8 kHz mu-law, which this
client forwards without a resample, and because barge-in is built on Deepgram
`SpeechStarted` (`vad_events=true`).

## Latency budgets

Measured from the code, not from a live call. `scripts/measure_voice_latency.py`
prints the same numbers and the selected providers. It refuses `--place-call`.

| Stage | Budget in code | Notes |
| --- | --- | --- |
| Deepgram connect | 5 s | `CONNECT_SECONDS` |
| Deepgram endpointing | 750 ms | silence before `speech_final` |
| Deepgram utterance end | 1000 ms | fallback flush |
| Reply settle | 100 ms after `speech_final`, else 300 ms | then the agent turn starts |
| LLM first text | 3 s | Nemotron stream or Gemini. Preparation cap 15 s |
| Local health check | 1.5 s | before Phonon or Nemotron falls back to cloud |
| Phonon first audio | after the full HTTP body | then 8000-byte chunks (1 s of mu-law) |
| ElevenLabs fallback | streaming WebSocket | first audio can arrive before the phrase ends |
| Auto IVR digit | no LLM, no TTS | after the STT endpoint, `<Play digits>` on the same Call SID |
| Model DTMF | LLM first text, then no speech if the reply is only `[/DTMF]` | wipe phrases are dropped |
| Barge-in | present | remote `SpeechStarted` or caller words during playback clear audio |

Recommended phone defaults:

- Keep streaming Deepgram (`interim_results`, `vad_events`, endpointing 750).
- Keep TTS phrases to one short sentence so Phonon's full-body wait stays small.
- Leave barge-in on. Do not disable `vad_events`.
- Prefer Phonon and Nemotron when their base URLs are healthy. Keep ElevenLabs
  and Gemini keys for fallback. Do not move the trunk off Twilio.

Bottleneck: Phonon time-to-first-byte is the full utterance synthesis, not the
first chunk. Strict brevity is the mitigation in this change. Streaming the
Phonon response body is still open.

## Threat model

- Secrets: PIN, SSN last four, and any password. Passwords are not a field.
  Research files and `repr` must not contain the other two. The live model
  instruction does, in process memory, for one reply.
- Allowlist: the pack destination must match `to`, and `to` must be allowlisted.
- Unattended dial: two flags, mission goal, not CI, relay path only.
- DTMF account wipe: auto digits only for a representative digit just spoken.
  "all lines" / "whole account" blocks digits.
- Logs: digit count, note kind, provider name. Not digits, not PIN, not SSN.

## Gaps

Unattended native conference, streaming Phonon, an X research refresh, and a
hard block on every model-chosen digit beyond the wipe list are not in this
change.
