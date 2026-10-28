# T-Mobile cancel sources

Curated 2026-10-28. Community and press reports are unverified. They are not
T-Mobile promises and not legal advice. Do not put a PIN, SSN, OTP, or
password in this file. Account and line numbers used by the mission live in
the pack, not here.

X post search was still rate-limited on 2026-10-28. Refresh public X posts
after 2026-10-29 00:00 UTC. Do not add secrets or legal claims from those posts.

## Official

| Id | Source | What we use |
| --- | --- | --- |
| S1 | [Cancel service](https://www.t-mobile.com/support/account/cancel-service) | Cancel is future-dated and typically takes effect at the end of the billing cycle. If other lines stay active, EIP on a canceled line continues to bill monthly. Canceling the whole account makes remaining device balances due on the final bill. Promos, free lines, and multi-line discounts may change. Unpair a smartwatch line before cancel. Voicemail and DIGITS history are deleted. The page also asks you to call first. |
| S2 | [Contact us](https://www.t-mobile.com/contact-us) | Care is 611 from a T-Mobile phone, or 1-800-937-8997. Representatives are daily 4 a.m. to 9 p.m. Pacific. Twilio cannot dial 611, so this mission dials +18009378997 only when that number is allowlisted. |

## Recorded IVR

| Id | Source | What we use |
| --- | --- | --- |
| S3 | [call4.me, October 2026 recording of 1-800-937-8997](https://call4.me/blog/t-mobile-customer-service-number) | Third-party recording, not an official menu map. Menus change. On that call, the mobile-number prompt accepted "I don't have one" twice. Saying "representative" alone did not transfer. The menu then offered payment, tech support, billing, or something else, and "something else" reached a person. Prefer DTMF only when the recording just offered "press N" for a representative. |

## Community and press (unverified)

| Id | Source | What we use |
| --- | --- | --- |
| S4 | [Wireless Dealer Group, October 2025](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) | Unverified. Be direct, decline retention, repeat the cancel request, ask for a confirmation number, and ask for a supervisor once if they resist. Do not give a long reason, do not agree to think about it, and do not take a callback instead of the cancel. |
| S5 | [Android Police, September 2025](https://www.androidpolice.com/t-mobile-smartwatch-tablet-line-cancellation-difficulty/) | Unverified. Watch and tablet lines often need a phone call. Reports include long retention pitches, a supervisor, and a $10 credit for six months. |
| S6 | [PhoneArena, watch-line cancel reports](https://www.phonearena.com/news/t-mobile-customers-spending-hours-trying-to-do-something-that-should-take-minutes_id174462) | Unverified. Reports of circles, upsells, and supervisor transfers when disconnecting a watch. Refuse a cheaper-plan conversion. |
| S7 | [PhoneArena, port-out versus verbal cancel](https://www.phonearena.com/news/reps-have-incentive-to-lie-when-you-cancel-service_id180362) | Unverified. Porting can close a line you are leaving. This mission keeps other lines, including Beata's, so it does not port out the whole account. Still require a confirmation number and an effective date. |
| S8 | [r/tmobile retention offers](https://www.reddit.com/r/tmobile/comments/1kgb1j8/retention_offers_for_cancelling_lines/) | Unverified. Reports include about $20 a month for six months and a "you'll never get an offer again" scare. Ignore the scare unless the pack policy accepts the credit. |
| S9 | [r/tmobile rep tricks](https://www.reddit.com/r/tmobile/comments/1jzsury/tmobile_reps_trick_customers_into_not_canceling/) | Unverified. One report says a watch-line credit was applied by downgrading another voice line. Another says a rep claimed EIP blocked a watch cancel. Refuse any change to another line. If they claim EIP blocks the cancel, restate the cancel and ask for a supervisor. S1 says EIP can keep billing while other lines stay active. |
| S10 | [r/tmobile watch lines after a port](https://www.reddit.com/r/tmobile/comments/1jx9rww/how_do_i_cancel_my_watch_lines/) | Unverified. Watch lines can keep billing after the phone number is ported. A PIN alone may fail when a one-time code cannot be received. |
| S11 | [r/tmobile retention and end of cycle](https://www.reddit.com/r/tmobile/comments/1ou1009/removing_a_lineshady_retention_department/) | Unverified. Comments describe a retention department and end-of-cycle line cancels. Ask for an effective date. This matches S1's "typically end of the billing cycle" wording. It is not a guarantee. |
| S12 | [TMO Report, 2023 retention promo leak](https://tmo.report/2023/03/leaked-document-shows-retention-promo-for-customers-thinking-of-leaving-t-mobile/) and the 2025 loyalty summaries already linked in RESEARCH.md | Unverified. Older reports of roughly $10 a month for six months and $20 a month for twelve months. The pack policy decides. The default declines a six-month credit. |

## Principles

| Situation | Action | Line |
| --- | --- | --- |
| Open | Direct cancel of the named line | `open-cancel` or `open-watch` |
| Why are you leaving | Broken record, no story | `broken-record` |
| Credit below the pack threshold | Decline, restate cancel, ask for confirmation | `decline-offer`, `cancel-line`, `ask-confirm` |
| Credit meets the pack threshold | Accept only that lasting monthly credit | `accept-retention` |
| Credit that downgrades or converts another line | Refuse | `no-other-line`, `cancel-line` |
| Think about it, or a callback | Refuse | `no-callback`, `ask-confirm` |
| EIP blocks the cancel | Push back, then supervisor once | `eip-still-cancel`, `supervisor` |
| EIP will keep billing, and they are not blocking the cancel | Still cancel | `eip-ok` |
| Unpair the watch | Say so, then continue | `unpair-watch` |
| Cancel refused | Supervisor once | `supervisor` |
| After they cancel | Confirmation number and effective date, then readback | `ask-confirm`, `readback` |
| Missing PIN, SSN, or OTP | Do not invent one | `need-holder`, then hang up |
| IVR number prompt | S3 route | `ivr-no-number` twice, then `ivr-something-else` |
| Whole account, port, or suspend | Named lines only | `only-named`, `leave-others` |
