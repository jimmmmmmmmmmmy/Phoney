"""Build 2: a signed Twilio switchboard with optional passive audio capture."""

import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from twilio.twiml.voice_response import VoiceResponse

from config import Settings
from webhooks import require_sid, twilio_validator, valid_media_signature
from switchboard.service import SessionRejected, Switchboard
from media_capture import CaptureManager

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


def create_app(settings: Settings, gateway=None) -> FastAPI:
    media_capture = CaptureManager(settings)
    switchboard = Switchboard(settings, gateway=gateway, on_end=media_capture.finish)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await switchboard.close()
        await media_capture.close()

    app = FastAPI(title="Passive Operator — Build 2", docs_url=None, redoc_url=None,
                  openapi_url=None, redirect_slashes=False, lifespan=lifespan)
    app.state.switchboard = switchboard
    app.state.media_capture = media_capture
    validate_twilio = twilio_validator(settings)

    @app.get("/health")
    @app.get("/")
    async def health():
        result = {"status": "ok", "service": "passive-operator", "build": 2,
                  "switchboard_ready": settings.switchboard_ready,
                  "media_capture_enabled": settings.media_capture_enabled and settings.switchboard_ready}
        if settings.deploy_commit:
            result["commit"] = settings.deploy_commit
        return result

    async def validate_deploy_control(request: Request):
        if not settings.deploy_control_token:
            raise HTTPException(503, "Deployment control is not configured")
        expected = "Bearer " + settings.deploy_control_token
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(),
                                   expected.encode()):
            raise HTTPException(403, "Invalid deployment control token")

    def deploy_state():
        return JSONResponse({"draining": switchboard.draining,
                             "active_sessions": switchboard.active_count,
                             "pending_work": switchboard.pending_count + media_capture.active_count
                                             + media_capture.pending_count},
                            headers={"Cache-Control": "no-store"})

    @app.get("/internal/deploy", dependencies=[Depends(validate_deploy_control)])
    async def deploy_status():
        return deploy_state()

    @app.post("/internal/deploy", dependencies=[Depends(validate_deploy_control)])
    async def deploy_drain(request: Request):
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected a draining boolean") from None
        if (not isinstance(payload, dict) or set(payload) != {"draining"}
                or not isinstance(payload["draining"], bool)):
            raise HTTPException(400, "Expected a draining boolean")
        await switchboard.set_draining(payload["draining"])
        return deploy_state()

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
        call_sid = require_sid(form.get("CallSid"))
        response = VoiceResponse()
        if settings.switchboard_ready:
            if form.get("To") != settings.twilio_number:
                raise HTTPException(400, "Unexpected destination")
            try:
                session = await switchboard.start(call_sid, str(form.get("From", "")))
            except SessionRejected:
                response.say("The team is unavailable right now. Please try again later.")
                response.hangup()
                return Response(str(response), media_type="application/xml")
            if session.phase == "ended":
                response.hangup()
                return Response(str(response), media_type="application/xml")
            response.say("New College Data Science Team", language="en-US")
            if settings.media_capture_enabled:
                try:
                    ticket = media_capture.reserve(call_sid)
                except (ValueError, RuntimeError, OSError) as exc:
                    # Passive capture must not prevent the two humans talking.
                    logger.error("capture_reservation_failed type=%s", type(exc).__name__)
                else:
                    response.say("This demo call records audio for testing.", language="en-US")
                    stream = response.start().stream(
                        url=settings.public_base_url.replace("https://", "wss://", 1)
                            + f"/media/{call_sid}/",
                        name=ticket.stream_name,
                        track="both_tracks",
                        status_callback=settings.public_base_url + f"/media/status/{call_sid}",
                        status_callback_method="POST",
                    )
                    stream.parameter(name="token", value=ticket.token)
            dial = response.dial(
                action=settings.public_base_url + f"/conference/finished/{call_sid}",
                method="POST",
            )
            dial.conference(
                session.conference_name,
                participant_label="caller",
                start_conference_on_enter=False,
                end_conference_on_exit=True,
                beep=False,
                max_participants=2,
                status_callback=settings.public_base_url + f"/conference/events/{call_sid}",
                status_callback_method="POST",
                status_callback_event="start end join leave",
            )
            return Response(str(response), media_type="application/xml")
        logger.info("unconfigured_voice call_sid=%s", call_sid)
        response.say("New College Data Science Team", language="en-US")
        response.hangup()
        return Response(str(response), media_type="application/xml")

    @app.websocket("/media/{parent_call_sid}/")
    async def media_socket(websocket: WebSocket, parent_call_sid: str):
        try:
            require_sid(parent_call_sid)
        except HTTPException:
            await websocket.close(code=1008)
            return
        session = switchboard.sessions.get(parent_call_sid)
        if (not settings.media_capture_enabled or not session or session.phase == "ended"
                or not valid_media_signature(settings, websocket)):
            logger.warning("media_handshake_rejected call_sid=%s", parent_call_sid)
            await websocket.close(code=1008)
            return
        await media_capture.handle(websocket, parent_call_sid)

    @app.post("/media/status/{parent_call_sid}")
    async def media_status(parent_call_sid: str, form=Depends(validate_twilio)):
        require_sid(parent_call_sid)
        if require_sid(form.get("CallSid")) != parent_call_sid:
            raise HTTPException(400, "Stream callback does not match the call")
        stream_sid = require_sid(form.get("StreamSid"), "MZ")
        event = str(form.get("StreamEvent", ""))
        if event not in {"stream-started", "stream-stopped", "stream-error"}:
            raise HTTPException(400, "Unknown stream event")
        if not form.get("StreamName"):
            raise HTTPException(400, "Missing stream name")
        media_capture.mark_status(parent_call_sid, stream_sid, event,
                                  stream_name=str(form.get("StreamName", "")),
                                  error="twilio_stream_error" if form.get("StreamError") else None)
        return Response(status_code=204)

    @app.post("/conference/events/{parent_call_sid}")
    async def conference_events(parent_call_sid: str, form=Depends(validate_twilio)):
        require_sid(parent_call_sid)
        require_sid(form.get("ConferenceSid"), "CF")
        if form.get("StatusCallbackEvent") in {"participant-join", "participant-leave"}:
            require_sid(form.get("CallSid"))
        try:
            await switchboard.conference_event(parent_call_sid, form)
        except ValueError:
            raise HTTPException(400, "Conference callback does not match the session") from None
        return Response(status_code=204)

    @app.post("/calls/status/{parent_call_sid}")
    async def call_status(parent_call_sid: str, form=Depends(validate_twilio)):
        require_sid(parent_call_sid)
        require_sid(form.get("CallSid"))
        try:
            await switchboard.call_status(parent_call_sid, form)
        except ValueError:
            raise HTTPException(400, "Call callback does not match the session") from None
        return Response(status_code=204)

    @app.post("/conference/finished/{parent_call_sid}")
    async def conference_finished(parent_call_sid: str, form=Depends(validate_twilio)):
        require_sid(parent_call_sid)
        if require_sid(form.get("CallSid")) != parent_call_sid:
            raise HTTPException(400, "Call callback does not match the session")
        await switchboard.finished(parent_call_sid)
        response = VoiceResponse()
        response.hangup()
        return Response(str(response), media_type="application/xml")

    @app.post("/status")
    async def status(form=Depends(validate_twilio)):
        require_sid(form.get("CallSid"))
        # Optional callback, kept minimal for the next milestone.
        call_status = str(form.get("CallStatus", "unknown"))
        if call_status not in {"queued", "initiated", "ringing", "in-progress", "completed",
                               "busy", "failed", "no-answer", "canceled"}:
            call_status = "unknown"
        logger.info("call_status call_sid=%s status=%s", form["CallSid"], call_status)
        return Response(status_code=204)

    return app
