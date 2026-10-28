# T-Mobile cancel mission research

Curated on 2026-10-28 from public pages. This is not legal advice and not a
promise that a representative will offer anything. Community reports are
unverified. The mission policy object decides whether an offer is acceptable.
Never put a PIN, SSN, or password in this file.

## TODO — X

X post search hit the daily read limit on 2026-10-28. Refresh public X
cancel and retention posts after 2026-10-29 00:00 UTC. Do not copy secrets
into this file. Do not add legal claims from social posts.

## Official

### Cancel timing and side effects

Source: <https://www.t-mobile.com/support/account/cancel-service>

T-Mobile's support page says:

- Cancellations are future dated and typically take effect at the end of the current billing cycle.
- If you cancel a line but keep other active lines, device payments tied to the canceled line continue to bill monthly. If the entire account is canceled, remaining device balances become due on the final bill.
- Canceling lines may affect free or discounted line promotions, device promotional credits, multi-line discounts, and rate plans that require a certain number of active lines.
- Smartwatch lines paired to a phone line should be unpaired before cancellation to avoid issues with future device use.
- Voicemail messages and DIGITS call history are permanently deleted after cancellation and cannot be recovered.

Those are T-Mobile's support statements. This repository does not add a legal interpretation.

### How to call

Source: <https://www.t-mobile.com/contact-us>

- Customer Service: dial 611 from a T-Mobile phone, or call 1-800-937-8997.
- Customer Care Representatives are available daily from 4 a.m. to 9 p.m. Pacific Time.
- Twilio cannot dial 611. This mission dials +18009378997 only when that number is on `ALLOWED_DESTINATIONS`.

## Community tactics

These are unverified reports. Offers change by plan, tenure, and representative.
Do not treat the dollar amounts as a T-Mobile commitment.

### Loyalty / retention

Reported in August 2025 and summarized by:

- <https://www.phonearena.com/news/t-mobile-might-be-quietly-rewarding-loyal-customers_id172936>
- <https://www.talkandroid.com/513571-t-mobile-loyalty-perks-reward/>
- <https://www.androidauthority.com/t-mobile-loyalty-bill-credits-offer-3584330/>
- <https://www.androidpolice.com/t-mobile-discount-trick-retain-unhappy-customers/>

Those articles describe a Reddit post attributed to JcAo2012: a loyalty team,
described as the newer retention path, may offer about $10 a month for 6 months,
and about $20 a month for 12 months on some higher-tier plans, sometimes only
after a coach or manager is involved. The articles also say some people are
given cancellation steps and no discount.

This mission does not hard-code those amounts. Default policy accepts a lasting
monthly reduction only when it is at least the pack's
`min_monthly_reduction_cents` and lasts at least `min_months` (or the
representative says it does not expire). A 6-month credit is below the default
12-month minimum. A one-time credit is declined. Otherwise the named lines
are cancelled.

### Watch lines and porting

Community threads, not T-Mobile policy:

- <https://www.reddit.com/r/tmobile/comments/1jx9rww/how_do_i_cancel_my_watch_lines/> — the poster said the website would not cancel watch lines after the phone lines were ported. Commenters suggested a store visit, porting the watch line, or another identity check. A PIN alone may not be enough when a one-time code cannot be received.
- <https://www.reddit.com/r/tmobile/comments/1jn4pkv/watch_line_canceled_or_do_i_need_to_call/> — commenters said porting the phone number does not cancel a separate watch number.
- <https://www.reddit.com/r/tmobile/comments/1cp3y34/if_i_port_out_my_number_does_it_cancel_my/> — same community claim: the watch line still needs its own cancel or port.
- <https://www.reddit.com/r/tmobile/comments/183ltu7/does_porting_out_all_voice_lines_automatically/> — commenters said a DIGITS watch line keeps billing if it was not canceled.

The agent still asks to cancel each named line. It does not assume a phone port canceled the watch.

### IVR

<https://call4.me/blog/t-mobile-customer-service-number> is a recorded walkthrough of 1-800-937-8997, not an official menu map. Menus change. In that recording, saying "representative" did not transfer; the menu then offered payment, tech support, billing, or something else, and "something else" reached a person. "I don't have one" was how that caller passed a mobile-number prompt.

On this mission, prefer DTMF when the recording just offered a digit for a representative. If the menu asks for the mobile number, send that line's digits rather than a paragraph. Do not press a digit that would cancel every line or the whole account. Say "Something else." or "Cancel a line." only when no safe digit was offered. Each of those lines is one short utterance in `utterances.json`.

## Spoken lines

`utterances.json` is the agent bank. Each line is one short sentence. The brief
may list them. The model says at most one per turn. They are curated for this
agent. They are not official T-Mobile scripts.

Opening intent: "I'd like to cancel the line ending in XXXX." and "Cancel the Apple Watch cellular line."

Pushback, split so each turn stays one sentence: "I'm not interested in offers." then "Please cancel line [number]." Ask for the confirmation number and effective date. If cancellation is refused, "Please transfer me to a supervisor."
