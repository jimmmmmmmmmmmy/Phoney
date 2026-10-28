# T-Mobile cancel conversation tree

What they say maps to a canned action. Each spoken id is one short sentence in
`utterances.json`. The runtime uses this map for covered forks and does not
call the model. An unmatched menu may still get one short recovery sentence.
This file has no PIN, SSN, OTP, or password.

Account number and line numbers below are the mission pack's identity, not
new secrets. PIN and SSN last four are spoken only when the private env has
them. Otherwise the call ends with `need-holder`.

| They say | Action | Utterance or DTMF |
| --- | --- | --- |
| Press N for a representative | DTMF that digit | playbook auto |
| Press N to cancel every line / the whole account | Speak, do not press | `ivr-representative` |
| Para espanol / which language | Speak | `ivr-english` |
| Enter or say your mobile number, and a line is known | DTMF that line | current pack line |
| Mobile number prompt, no line known | Speak | `ivr-no-number` |
| Payment, billing, tech, or something else, no safe digit | Speak | `ivr-something-else` or `ivr-cancel` |
| Welcome to T-Mobile, no menu digit | Speak | `ivr-cancel` |
| Please hold / your call is important | Silence | no speech |
| Wrong department or a transfer back | Speak | `wrong-department` |
| Enter or dial the account number | DTMF | pack account number |
| What is the account number | Speak | `say-account` |
| Account PIN or passcode, env set | Speak once | `say-pin` |
| Account PIN or passcode, env empty | Speak, then hang up | `need-holder` |
| SSN or last four, env set | Speak once | `say-ssn` |
| SSN or last four, env empty | Speak, then hang up | `need-holder` |
| One-time code, verification code, or OTP | Speak, then hang up | `need-holder` |
| Account name | Speak | `say-name` |
| Billing ZIP, pack has `billing_zip` | Speak | `say-zip` |
| Billing ZIP, pack has none | Speak, then hang up | `need-holder` |
| How can I help, current line is a watch | Speak | `open-watch` |
| How can I help, other line | Speak | `open-cancel` |
| Which line | Speak | `which-line` |
| Whole account, port out, or suspend | Speak | `only-named`, then `leave-others` |
| Cheaper plan or cheaper line | Speak | `decline-plan` |
| You'll never get this offer again | Speak | `cancel-line` |
| Multi-line discount or promo will change | Speak | `promo-still-cancel` |
| EIP or device payments will continue | Speak | `eip-ok` |
| Monthly offer below pack threshold | Speak | `decline-offer`, `cancel-line`, `ask-confirm` |
| Monthly offer at or above pack threshold | Speak, note, hang up | `accept-retention`, `goodbye` |
| One-time credit | Speak | same decline path |
| Confirmation number and effective date, another line remains | Speak, note | `readback`, then `next-line` |
| Confirmation number and effective date, both lines done | Speak, note, hang up | `readback`, `goodbye` |
| Cannot cancel, supervisor not asked yet | Speak | `supervisor` |
| Cannot cancel again | Speak, hang up | `partial-close`, `goodbye` |
| Unpair the watch | Speak | `unpair-watch` |
| Phone port already canceled the watch | Speak | `watch-separate` |

Default policy numbers live in `phoney_missions/packs/tmobile-cancel.json`:
a lasting cut of at least $20.00 for at least 12 months. A reported $10 for 6
months is declined. A reported $20 for 12 months may be accepted. Those
community amounts are not promises. See `RESEARCH.md`.
