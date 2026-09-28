"""Preserve inbound caller ID using Twilio's immutable forwarding token."""

import re

_E164 = re.compile(r"\+[1-9]\d{1,14}\Z")


def forwarding_identity(twilio_number: str, caller_number: str = "", call_token: str = ""):
    # Only a signed inbound webhook supplies these values. Without both a
    # usable number and its token, retain the owned number so calls still ring.
    # Never substitute an unverified From alone or log the forwarding token.
    if call_token and _E164.fullmatch(caller_number):
        return {"from_": caller_number, "call_token": call_token}
    return {"from_": twilio_number}
