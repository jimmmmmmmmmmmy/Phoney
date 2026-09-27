# Manual continuation and turn handling — September 27, 2026

Manual Voice Clone transfers now continue until the caller explicitly ends or
confirms they are finished. The internal automatic AI-screening agent still has
three completed replies. Voicemail reads back a message and waits for confirmation.

## Provider evaluation

Four bounded runs made **55 Gemini requests** using fictional conversations,
Gemini 3.5 Flash-Lite with MINIMAL thinking, and the phone's 3-second first-text
deadline. No phone was called and no ElevenLabs audio was generated.

The final manual sequence continued on replies 1–4, retained corrected mileage,
and ended on reply 5 after the caller explicitly said goodbye. All five passed.
Across its accompanying nine-reply run, every continue/end action was correct.
Successful first-text times were 493–4,749 ms (median 812 ms); the slow cases
included a retry after the 3-second deadline. These are text timings, not audible
phone latency.

Semantic review found that one automatic response invented the owner's appointment
preference despite passing the original checker. The automatic workflow's final
instruction and checker were strengthened. A targeted three-reply recheck produced
a neutral first question and the expected final goodbye, but the second reply
exceeded both first-text deadlines. That is a provider failure, not a passing
conversation. Earlier runs also include a request-cap failure, provider timeouts,
overly strict checks, and a voicemail promise corrected with a prescribed closing.
There is no claim that all live provider checks passed.

The [machine-readable report](voice-prompt-continuation-evaluation-2026-09-27.json)
retains all runs and the semantic finding. The report records historical checks
as run. A timeout before headers could previously inherit the preceding request's
diagnostic metadata; the harness now resets it at request start.

## Runtime changes

- Finalized transcript chunks remain saved immediately. Replies wait for a
  speech endpoint rather than treating every stable chunk as a completed turn.
- Caller activity cancels queued responses and revokes stale hangups, including
  automatic screening's third reply and terminal voicemail phases.
- New caller details received during generation remain available for follow-up.
- A silent Gemini attempt retries once after 3 seconds; partial spoken output is
  never replayed. Normal stream deadlines apply after text begins. Initial
  takeover preparation remains capped at 15 seconds.

Model availability remains variable. Manual provider failures restore the human
bridge; voicemail failures retain the basic recording fallback. These tests do
not verify carrier audio, acoustically audible start time, or perfect model
wording. A real phone acceptance test is still required.

## Reproduce

```sh
.venv/bin/python scripts/evaluate_voice_prompts.py
.venv/bin/python scripts/evaluate_voice_prompts.py --live --max-requests 16 \
  --scenario manual_continues_until_finished \
  --scenario automatic_context_and_no_commitment \
  --scenario voicemail_unconfirmed_timeout \
  --output .runtime/prompt-evaluations/continuation.json
```
