"""Twilio authentication shared by call and conference webhook routes."""

import re

from fastapi import HTTPException, Request
from twilio.request_validator import RequestValidator


def valid_media_signature(settings, websocket):
    """Media Streams signs the fixed public WSS URL, without form fields."""
    if websocket.scope.get("query_string"):
        return False
    path = websocket.scope.get("raw_path", b"/").decode("ascii")
    origin = settings.public_base_url.replace("https://", "wss://", 1)
    return RequestValidator(settings.auth_token).validate(
        origin + path, {}, websocket.headers.get("x-twilio-signature", ""))


def require_sid(value, prefix="CA"):
    if not re.fullmatch(prefix + r"[0-9a-fA-F]{32}", str(value or "")):
        raise HTTPException(400, f"Missing or invalid {prefix} SID")
    return str(value)


def twilio_validator(settings):
    validator = RequestValidator(settings.auth_token)

    async def validate(request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise HTTPException(415, "Expected a Twilio form webhook")
        form = await request.form()
        # Use the configured public origin; forwarded headers are not trusted input.
        path = request.scope.get("raw_path", b"/").decode("ascii")
        query = request.scope.get("query_string", b"").decode("ascii")
        url = settings.public_base_url + path + ("?" + query if query else "")
        if not validator.validate(url, form, request.headers.get("x-twilio-signature", "")):
            raise HTTPException(403, "Invalid Twilio signature")
        if form.get("AccountSid") != settings.account_sid:
            raise HTTPException(403, "Unexpected Twilio account")
        return form

    return validate
