# Focused test suite

Run the complete suite:

```sh
.venv/bin/python -m pytest -q
```

The suite contains **96 collected cases across 81 test functions**. Parametrized
variants count toward the limit. The deployment supervisor runs this same suite,
not a separate subset. The first local run after consolidation finished in
15.5 seconds: 93 passed and three PostgreSQL cases skipped.

| Area | Cases | Behaviors checked |
| --- | ---: | --- |
| Calling and live evidence | 31 | Browser and phone call setup, signed session binding, hangup races, AI takeover and release, automatic detection handoff, caller barge-in, transcription, recordings, detection recovery, and voicemail. |
| Browser and dashboard | 24 | Real HTTP asset loading and CSP, actual vendored SDK export, media setup and cancellation, codec status, keypad controls, disconnect cleanup, authentication loss, playback, network recovery, and CRM edits. |
| Auth, storage, setup, and deployment | 31 | PIN lockout and recovery, session expiry, hashed credentials, signed HTTP/WebSocket callbacks, workspace persistence and isolation, JWT grants, Twilio App configuration, deployment draining, rollback, and commit health. |
| Saved call products | 10 | History and exports after restart, pagination, stereo playback, recording headers, detailed/brief summaries and retries, voicemail recording callbacks, and shared notifications. |

## Test structure

`tests/test_*.py` contains the scenarios. `tests/support/` contains only the
reachable fixtures, provider fakes, and browser harnesses those scenarios need.
Support modules contain no hidden test functions or collection filters. The
retained scenarios and fake behavior were preserved during extraction.

The asset check fetches every script URL from the actual dashboard response. The
CSP check inspects the policy returned by the HTTP route. Both fail when the SDK
route or required Twilio connection permission is removed. Node harnesses cover
UI behavior and SDK integration; they do not validate physical iPhone playback,
browser autoplay, carrier quality, or provider accuracy.

## Database checks

Three cases require `PHONEY_TEST_DATABASE_URL` to point at an isolated PostgreSQL
test database. Without it, those cases skip explicitly. The suite does not use
the installed production database as a substitute.

## Keep the suite small

Keep the complete suite below **100 collected cases**, including fixture and
parametrization variants. Add coverage for a distinct product behavior or an
observed regression. Replace an obsolete or redundant check when the budget is
full. Do not conceal unrelated input matrices inside loops or exclude a second
test archive from normal collection.

This is deliberately narrower coverage than the previous 2,687-case suite.
Repeated implementation assertions and broad input matrices were removed; some
unique edge-case checks were also removed to meet the requested limit. The full
previous suite remains recoverable in Git at commit `67c0bd4`.
