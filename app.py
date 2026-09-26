"""Build 0: a signed Twilio webhook that speaks and ends the call."""

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from config import Settings

logger = logging.getLogger("uvicorn.error")
MAX_GITHUB_BODY_BYTES = 1024 * 1024


def write_deploy_trigger(path: str, delivery_id: str) -> None:
    """Atomically notify the separate deploy worker without accepting deploy commands."""
    destination = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".deploy-trigger-", dir=destination.parent)
    try:
        # mkstemp creates a private 0600 file; replace preserves that mode.
        with os.fdopen(descriptor, "w", encoding="utf-8") as marker:
            json.dump({"received_at_ns": time.time_ns(), "delivery_id": delivery_id[:200]}, marker)
            marker.write("\n")
            marker.flush()
            os.fsync(marker.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
        result = {"status": "ok", "service": "passive-operator", "build": 0}
        if settings.deploy_commit:
            result["commit"] = settings.deploy_commit
        return result

    @app.post("/github/webhook")
    async def github_webhook(request: Request):
        if (not settings.github_webhook_secret or not settings.deploy_trigger_path
                or not settings.deploy_repository):
            raise HTTPException(503, "GitHub deployment webhook is not configured")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_GITHUB_BODY_BYTES:
                raise HTTPException(413, "GitHub webhook body is too large")
            body.extend(chunk)
        expected = "sha256=" + hmac.new(
            settings.github_webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        signature = request.headers.get("x-hub-signature-256", "")
        if not hmac.compare_digest(signature.encode("utf-8"), expected.encode("ascii")):
            raise HTTPException(403, "Invalid GitHub signature")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid GitHub JSON payload") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "GitHub payload must be an object")
        event = request.headers.get("x-github-event", "")
        if event == "ping":
            return {"status": "ok", "event": "ping"}
        if event != "push":
            return JSONResponse({"status": "ignored", "reason": "event"}, status_code=202)
        repository = payload.get("repository")
        if not isinstance(repository, dict) or repository.get("full_name") != settings.deploy_repository:
            return JSONResponse({"status": "ignored", "reason": "repository"}, status_code=202)
        if payload.get("ref") != "refs/heads/main":
            return JSONResponse({"status": "ignored", "reason": "branch"}, status_code=202)
        if payload.get("deleted", False):
            return JSONResponse({"status": "ignored", "reason": "deleted"}, status_code=202)
        try:
            write_deploy_trigger(settings.deploy_trigger_path,
                                 request.headers.get("x-github-delivery", ""))
        except OSError:
            logger.error("GitHub deployment trigger could not be written")
            raise HTTPException(503, "Deployment trigger is unavailable") from None
        return JSONResponse({"status": "accepted"}, status_code=202)

    @app.post("/voice")
    async def voice(form=Depends(validate_twilio)):
        logger.info("build0_voice call_sid=%s", form["CallSid"])
        response = VoiceResponse()
        response.say("New College Data Science Team", language="en-US")
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
