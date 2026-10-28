# T-Mobile cancel conversation tree

What they say maps to a canned action. Each spoken id is one short sentence in
`utterances.json`. The runtime uses this map and does not call the model for a
covered fork. Source ids are defined in [SOURCES.md](SOURCES.md). Community
ids S4–S12 are unverified. This file has no PIN, SSN, OTP, or password.

| They say | Action | Utterance or DTMF | Source |
| --- | --- | --- | --- |
| Press N for a representative | DTMF that digit | playbook auto | [S3](https://call4.me/blog/t-mobile-customer-service-number) |
| Press N to cancel every line or the whole account | Speak, do not press | `ivr-representative` | [S1](https://www.t-mobile.com/support/account/cancel-service), [S7](https://www.phonearena.com/news/reps-have-incentive-to-lie-when-you-cancel-service_id180362) |
| Para espanol or which language | Speak | `ivr-english` | [S3](https://call4.me/blog/t-mobile-customer-service-number) |
| Enter or say your mobile number, first or second time | Speak | `ivr-no-number` | [S3](https://call4.me/blog/t-mobile-customer-service-number) |
| Mobile number prompt after two "I don't have one" replies | DTMF that line | current pack line | [S3](https://call4.me/blog/t-mobile-customer-service-number) |
| Say representative, and no press-N digit was offered | Speak | `ivr-something-else` | [S3](https://call4.me/blog/t-mobile-customer-service-number) |
| Payment, billing, tech, or something else | Speak | `ivr-something-else` or `ivr-cancel` | [S3](https://call4.me/blog/t-mobile-customer-service-number) |
| Welcome to T-Mobile, no menu digit | Speak | `ivr-cancel` | [S3](https://call4.me/blog/t-mobile-customer-service-number) |
| Please hold or your call is important | Silence | no speech | [S3](https://call4.me/blog/t-mobile-customer-service-number), [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| Wrong department or a transfer back | Speak | `wrong-department` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| Enter or dial the account number | DTMF | pack account number | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| What is the account number | Speak | `say-account` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| Account PIN or passcode, env set | Speak once | `say-pin` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat), [S10](https://www.reddit.com/r/tmobile/comments/1jx9rww/how_do_i_cancel_my_watch_lines/) |
| Account PIN or passcode, env empty | Speak, then hang up | `need-holder` | [S10](https://www.reddit.com/r/tmobile/comments/1jx9rww/how_do_i_cancel_my_watch_lines/) |
| SSN or last four, env set | Speak once | `say-ssn` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| SSN or last four, env empty | Speak, then hang up | `need-holder` | [S10](https://www.reddit.com/r/tmobile/comments/1jx9rww/how_do_i_cancel_my_watch_lines/) |
| One-time code, verification code, or OTP | Speak, then hang up | `need-holder` | [S10](https://www.reddit.com/r/tmobile/comments/1jx9rww/how_do_i_cancel_my_watch_lines/) |
| Account name | Speak | `say-name` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| Billing ZIP, pack has `billing_zip` | Speak | `say-zip` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| Billing ZIP, pack has none | Speak, then hang up | `need-holder` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| How can I help, current line is a watch | Speak | `open-watch` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat), [S6](https://www.phonearena.com/news/t-mobile-customers-spending-hours-trying-to-do-something-that-should-take-minutes_id174462) |
| How can I help, other line | Speak | `open-cancel` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| Why are you leaving | Speak, no story | `broken-record` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| Which line | Speak | `which-line` | [S6](https://www.phonearena.com/news/t-mobile-customers-spending-hours-trying-to-do-something-that-should-take-minutes_id174462) |
| Whole account, port out, or suspend | Speak | `only-named`, then `leave-others` | [S7](https://www.phonearena.com/news/reps-have-incentive-to-lie-when-you-cancel-service_id180362), [S1](https://www.t-mobile.com/support/account/cancel-service) |
| Credit that downgrades or converts another line | Speak, do not accept | `no-other-line`, then `cancel-line` | [S9](https://www.reddit.com/r/tmobile/comments/1jzsury/tmobile_reps_trick_customers_into_not_canceling/) |
| Cheaper plan or cheaper line | Speak | `decline-plan` | [S6](https://www.phonearena.com/news/t-mobile-customers-spending-hours-trying-to-do-something-that-should-take-minutes_id174462) |
| You'll never get this offer again | Speak | `cancel-line` | [S8](https://www.reddit.com/r/tmobile/comments/1kgb1j8/retention_offers_for_cancelling_lines/) |
| Think about it, or a callback | Speak | `no-callback`, then `ask-confirm` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| Multi-line discount or promo will change | Speak | `promo-still-cancel` | [S1](https://www.t-mobile.com/support/account/cancel-service) |
| EIP will keep billing, and they are not blocking the cancel | Speak | `eip-ok` | [S1](https://www.t-mobile.com/support/account/cancel-service) |
| EIP or installment blocks the cancel | Speak, then supervisor once | `eip-still-cancel`, then `supervisor` | [S1](https://www.t-mobile.com/support/account/cancel-service), [S9](https://www.reddit.com/r/tmobile/comments/1jzsury/tmobile_reps_trick_customers_into_not_canceling/) |
| Monthly offer below the pack threshold, including about $10 for 6 months | Speak | `decline-offer`, `cancel-line`, `ask-confirm` | [S5](https://www.androidpolice.com/t-mobile-smartwatch-tablet-line-cancellation-difficulty/), [S8](https://www.reddit.com/r/tmobile/comments/1kgb1j8/retention_offers_for_cancelling_lines/), [S12](https://tmo.report/2023/03/leaked-document-shows-retention-promo-for-customers-thinking-of-leaving-t-mobile/) |
| Monthly offer at or above the pack threshold | Speak, note, hang up | `accept-retention`, `goodbye` | [S12](https://tmo.report/2023/03/leaked-document-shows-retention-promo-for-customers-thinking-of-leaving-t-mobile/) |
| One-time credit | Speak | same decline path | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| Confirmation number and effective date, another line remains | Speak, note | `readback`, then `next-line` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat), [S7](https://www.phonearena.com/news/reps-have-incentive-to-lie-when-you-cancel-service_id180362), [S11](https://www.reddit.com/r/tmobile/comments/1ou1009/removing_a_lineshady_retention_department/) |
| Confirmation number and effective date, both lines done | Speak, note, hang up | `readback`, `goodbye` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat), [S7](https://www.phonearena.com/news/reps-have-incentive-to-lie-when-you-cancel-service_id180362) |
| Cannot cancel, supervisor not asked yet | Speak | `supervisor` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat), [S5](https://www.androidpolice.com/t-mobile-smartwatch-tablet-line-cancellation-difficulty/) |
| Cannot cancel again | Speak, hang up | `partial-close`, `goodbye` | [S4](https://www.wirelessdealergroup.com/post/t-mobile-cancellation-tactics-anger-customers-aggressive-retention-strategy-makes-service-cancellat) |
| Unpair the watch | Speak | `unpair-watch` | [S1](https://www.t-mobile.com/support/account/cancel-service) |
| Phone port already canceled the watch | Speak | `watch-separate` | [S10](https://www.reddit.com/r/tmobile/comments/1jx9rww/how_do_i_cancel_my_watch_lines/) |

The pack policy is a lasting cut of at least $20.00 for at least 12 months.
Reported $10 for 6 months is declined. A reported $20 for 12 months may be
accepted. Those amounts are unverified community reports, not promises.
