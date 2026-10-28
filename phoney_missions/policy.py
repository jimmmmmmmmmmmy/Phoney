"""Retention versus cancel. The numbers come from the pack, not from prose."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .schema import MissionPack

_MONEY = re.compile(r"\$(\d+(?:\.\d{1,2})?)")
_ONE_TIME = re.compile(r"one[- ]time", re.IGNORECASE)
_PERMANENT = re.compile(
    r"permanent|permanently|ongoing|does not expire|no expiration|for the life",
    re.IGNORECASE,
)
_MONTHLY = re.compile(r"month|monthly|/mo", re.IGNORECASE)
_MONTHS = re.compile(r"(\d+)\s*months?", re.IGNORECASE)


@dataclass(frozen=True)
class RetentionDecision:
    is_offer: bool
    accept: bool
    monthly_cents: int
    months: int | None
    lasting: bool
    reason: str


def policy_text(pack: MissionPack) -> str:
    dollars = pack.retention.min_monthly_reduction_cents / 100
    return (
        f"Mission policy prefer={pack.prefer}. "
        f"Accept a retention offer only when it is a lasting monthly reduction "
        f"of at least {dollars:.2f} dollars for at least {pack.retention.min_months} months, "
        "or the representative says it does not expire. "
        "Decline one-time credits and shorter credits. "
        "Otherwise cancel only the named lines. "
        "Do not cancel any other line, and do not cancel the whole account."
    )


def evaluate_retention(text: str, pack: MissionPack) -> RetentionDecision:
    """Decide from the latest utterance. Community dollar amounts are not hardcoded."""
    if not isinstance(text, str) or "$" not in text:
        return RetentionDecision(False, False, 0, None, False, "no-offer")
    one_time = bool(_ONE_TIME.search(text))
    permanent = bool(_PERMANENT.search(text))
    best = None
    for match in _MONEY.finditer(text):
        cents = int(round(float(match.group(1)) * 100))
        window = text[max(0, match.start() - 24):match.end() + 48]
        monthly = bool(_MONTHLY.search(window))
        months_match = _MONTHS.search(text[match.end():match.end() + 48])
        months = int(months_match.group(1)) if months_match else None
        lasting = bool(permanent or (months is not None and months >= pack.retention.min_months))
        qualifies = bool(lasting and (monthly or permanent))
        if not monthly and not qualifies:
            continue
        if best is None or (qualifies, cents) > (best[2], best[0]):
            best = (cents, months, qualifies)
    if best is None:
        if one_time and pack.retention.decline_one_time_credits:
            return RetentionDecision(True, False, 0, None, False, "one-time")
        return RetentionDecision(False, False, 0, None, False, "no-monthly-offer")
    cents, months, lasting = best
    accept = bool(lasting and cents >= pack.retention.min_monthly_reduction_cents)
    reason = "accept" if accept else "below-policy"
    return RetentionDecision(True, accept, cents, months, bool(lasting), reason)
