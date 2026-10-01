# Telephone prompt evaluation — September 27, 2026

This report describes the earlier three-reply manual policy. Manual transfers now
continue until the caller is finished; see the later [continuation evaluation](VOICE_PROMPT_CONTINUATION.md).

The final prompt uses a shared telephone protocol, an editable Voice Clone
personality, and separate internal personalities for automatic AI screening and
voicemail. Three-reply workflows receive one explicit runtime step at a time.
Custom agents do not inherit an arbitrary three-reply limit.

## What was verified

- Manual Voice Clone: a question, a follow-up question, then goodbye and a valid
  unspoken `[/END CALL]` command.
- Automatic screening: inherited call context, two exchanges, then a neutral
  goodbye. The assistant does not tell the caller they were classified as AI.
- Voicemail: invite a message, read back the actual details after a runtime pause,
  ask for confirmation, and end after the caller confirms. A correction produces
  another readback. Silence itself is never confirmation.
- The caller can explicitly end early. Interrupted output does not advance the
  runtime reply counter. A quoted command in caller speech is not a tool command.

The live test used **Gemini 3.5 Flash-Lite, MINIMAL thinking, 2,048 output tokens**
with fictional transcripts. The final ten-reply sequence had the expected
continue/end action on **10 of 10 replies**. First text arrived in **421–663 ms**
(median **596 ms**). These are model-text timings, not time to audible speech.

Semantic review found one unsupported promise to relay information. The final
step now prescribes a neutral closing. Two separate targeted live checks then
passed: “Thanks for the information. Goodbye.” and “Thank you for leaving your
message. Goodbye.” Those checks were not a rerun of the full ten-reply sequence.

## Iterations and limits

There were **59 real Gemini requests** across all iterations. Initial prompt-only
instructions allowed premature farewells, so the runtime now supplies an explicit
current workflow step. Internal three-reply workflows also have application-level
end-call checks; the model alone does not control the deadline or playback.

The first harness used persistent provider-native model history and a 512-token
budget. That differed from the phone runtime and produced empty responses on
some second replies. The harness was corrected to rebuild an attributed
transcript packet before every reply, with the production token budget. No empty
response occurred in the final ten-reply production-context run. This is evidence
about the test harness, not a diagnosis of a production phone-call failure.

One initial semantic checker also rejected “I'll make sure that's noted.” A
voicemail is actually recorded locally, so this is different from promising that
the owner will receive, read, or respond to a forwarded message. The checker now
distinguishes those claims; the final prompt uses a neutral acknowledgment anyway.

The machine-readable [evaluation report](voice-prompt-evaluation-2026-09-27.json)
preserves the final sequence's original failures and the separate closing
rechecks. Earlier raw iteration reports are retained under the ignored local
`.runtime/prompt-evaluations/2026-09-27/` directory.

These tests do not call Twilio, synthesize audio, measure silence detection, or
disconnect a real phone. They exercise the production Gemini adapter,
attributed context, and command parser. Runtime tests separately verify that
hangup occurs only after the farewell has finished playing. A small model sample
cannot guarantee perfect behavior on every caller utterance.

## Reproduce

Run network-free protocol fixtures:

```sh
.venv/bin/python scripts/evaluate_voice_prompts.py
.venv/bin/python -m pytest -q tests/test_operator_keypad.py tests/test_operator_bounded_replies.py tests/test_operator_voicemail_integration.py
```

Run a bounded real-model sequence using the installed private environment file:

```sh
.venv/bin/python scripts/evaluate_voice_prompts.py --live --max-requests 12 \
  --scenario manual_continues_until_finished \
  --scenario automatic_context_and_no_commitment \
  --scenario voicemail_capture_readback_confirm \
  --scenario caller_ends_early \
  --output .runtime/prompt-evaluations/latest.json
```

`--live` is explicit. The default mode does not load credentials or make network
requests. Each run has a physical HTTP request limit, including retries, and
reports only fictional dialogue and safe provider metadata. Prompts are versioned
in `voice_stack/prompts.py`; runtime phase names are validated before use.
