"""Build 0: a signed Twilio webhook that speaks and ends the call."""

import logging
import re

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from config import Settings

logger = logging.getLogger("uvicorn.error")


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Passive Operator — Build 0", docs_url=None, redoc_url=None,
                  openapi_url=None, redirect_slashes=False)
    validator = RequestValidator(settings.auth_token)

    async def validate_twilio(request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise HTTPException(415, "Expected a Twilio form webhook")
        form = await request.form()
        # Use our configured public origin, never client-supplied Host/forwarded headers.
        path = request.scope.get("raw_path", b"/").decode("ascii")
        query = request.scope.get("query_string", b"").decode("ascii")
        url = settings.public_base_url + path + ("?" + query if query else "")
        signature = request.headers.get("x-twilio-signature", "")
        if not validator.validate(url, form, signature):
            raise HTTPException(403, "Invalid Twilio signature")
        if form.get("AccountSid") != settings.account_sid:
            raise HTTPException(403, "Unexpected Twilio account")
        if not re.fullmatch(r"CA[0-9a-fA-F]{32}", str(form.get("CallSid", ""))):
            raise HTTPException(400, "Missing or invalid CallSid")
        return form

    @app.get("/health")
    @app.get("/")
    async def health():
        return {"status": "ok", "service": "passive-operator", "build": 0}

    @app.post("/voice")
    async def voice(form=Depends(validate_twilio)):
        logger.info("build0_voice call_sid=%s", form["CallSid"])
        response = VoiceResponse()
        response.say("Operator online. Build zero is ready.", language="en-US")
        response.hangup()
        return Response(str(response), media_type="application/xml")

    @app.post("/status")
    async def status(form=Depends(validate_twilio)):
        # Optional callback, kept minimal for the next milestone.
        call_status = str(form.get("CallStatus", "unknown"))
        if call_status not in {"queued", "initiated", "ringing", "in-progress", "completed",
                               "busy", "failed", "no-answer", "canceled"}:
            call_status = "unknown"
        logger.info("call_status call_sid=%s status=%s", form["CallSid"], call_status)
        return Response(status_code=204)

    return app
