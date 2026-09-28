"""Signed Twilio calling, unanswered-call voicemail, and public live transcripts."""

import asyncio
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
from fastapi.responses import JSONResponse, RedirectResponse, Response
from twilio.twiml.voice_response import VoiceResponse

from config import Settings
from webhooks import require_sid, twilio_validator, valid_media_signature
from switchboard.service import SessionRejected, Switchboard
from media_capture import CaptureManager
from media_capture.playback import RecordingLibrary
from transcription import TranscriptionManager
from dashboard import register_dashboard
from workspace_store import WorkspaceStore
from voicemail import VoicemailStore
from call_details import CallDetailsStore
from summaries import SummaryManager
from operator_service import OperatorRejected, OperatorSessions, register_operator_routes
from partner_detection import LiveDetectionManager
from partner_detection.storage import DetectionStore
from partner_detection.backfill import BackfillManager
from agent_registry import (AgentRegistry, RegistryError, owner_authenticated, owner_write_access,
                            register_agent_routes)
from bridge_pipeline import BridgePipeline

logger = logging.getLogger("uvicorn.error")
MAX_GITHUB_BODY_BYTES = 1024 * 1024
DETECTION_SAVE_SHUTDOWN_SECONDS = 5.0


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


def create_app(settings: Settings, gateway=None, transcription_connector=None, summary_provider=None,
               operator_dialer=None, operator_voice=None, detection_connector=None,
               detection_batch_provider=None, agent_voice=None, agent_voice_provider=None) -> FastAPI:
    transcription = TranscriptionManager(settings, connector=transcription_connector)
    voicemails = VoicemailStore(settings)
    recordings = RecordingLibrary(settings)
    call_details = CallDetailsStore(settings.call_details_storage_dir)
    operator = OperatorSessions(settings)
    detection_store = DetectionStore(settings.detection_storage_dir)
    workspace_store = WorkspaceStore(settings.workspace_storage_dir)
    agent_registry = AgentRegistry(settings.workspace_storage_dir)
    detection_writes: set[asyncio.Task] = set()
    detection_last_write: dict[str, asyncio.Task] = {}

    def persist_detection(call_sid, result):
        previous = detection_last_write.get(call_sid)

        async def save():
            # Preserve start/final/reconnect order even when disk work runs on threads.
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            try:
                await asyncio.to_thread(detection_store.save, call_sid, result)
            except Exception:
                logger.error("detection_result_save_failed")

        task = asyncio.create_task(save(), name="save-detection-result")
        detection_writes.add(task)
        detection_last_write[call_sid] = task

        def saved(finished):
            detection_writes.discard(finished)
            if detection_last_write.get(call_sid) is finished:
                detection_last_write.pop(call_sid, None)

        task.add_done_callback(saved)
        # Only live provider events can control a current call. Reading stored
        # analysis or recording backfill must never activate a phone agent.
        session = bridge_pipeline.calls.get(call_sid)
        if session is not None:
            controller.on_detection(session.id, result)

    async def call_ended(call_sid):
        await asyncio.to_thread(call_details.finish, call_sid)
        voicemails.finish(call_sid)
        await media_capture.finish(call_sid)

    switchboard = Switchboard(settings, gateway=gateway, on_end=call_ended)

    def provider_worker_active():
        if switchboard.draining:
            return False
        if not settings.deploy_commit:
            return True
        # Candidate releases share private storage and credentials. Only the
        # supervisor-owned serving process may start billable background jobs.
        if not settings.deploy_trigger_path:
            return False
        try:
            record = json.loads((Path(settings.deploy_trigger_path).parent / "dev.json").read_text())["app"]
            return record["pid"] == os.getpid() and record.get("commit") == settings.deploy_commit
        except (OSError, ValueError, KeyError, TypeError):
            return False

    live_detection = LiveDetectionManager(settings, connector=detection_connector,
        can_run=provider_worker_active, on_update=persist_detection)
    media_capture = CaptureManager(settings, observer=(transcription, live_detection))
    bridge_pipeline = BridgePipeline(settings, media_capture, transcription, live_detection, call_details, voicemails, recordings)
    transcription.on_segment = bridge_pipeline.transcript_event
    transcription.on_failure = bridge_pipeline.transcription_failed
    summaries = SummaryManager(settings, transcription, call_details,
        active_call_ids=lambda: ({sid for sid, session in switchboard.sessions.items() if session.phase != "ended"}
                                | bridge_pipeline.active_call_ids),
        provider=summary_provider, can_run=provider_worker_active)
    detection_backfill = BackfillManager(settings, detection_store,
        active_call_ids=lambda: (
            {sid for sid, session in switchboard.sessions.items() if session.phase != "ended"}
            | bridge_pipeline.active_call_ids | live_detection.active_call_ids | set(detection_last_write)),
        provider=detection_batch_provider, can_run=provider_worker_active)

    @asynccontextmanager
    async def lifespan(app):
        if settings.agent_demo_mode:
            try:
                await asyncio.to_thread(agent_registry.ensure_default_voice_clone)
            except RegistryError:
                logger.warning("default_voice_clone_unavailable")
        summaries.start()
        detection_backfill.start()
        yield
        await detection_backfill.close()
        await summaries.close()
        await switchboard.close()
        for session in list(operator.sessions.values()):
            if session.active:
                await controller.end(session.id, "server-shutdown")
        await operator.close()
        await bridge_pipeline.close()
        await media_capture.close()
        await live_detection.close()
        if detection_writes:
            _, pending = await asyncio.wait(list(detection_writes), timeout=DETECTION_SAVE_SHUTDOWN_SECONDS)
            if pending:
                logger.error("detection_result_shutdown_timeout")
                # No new callbacks can be created after close. Cancel the whole
                # pending chain together so queued writes cannot overtake it.
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        await transcription.close()
        await voicemails.close()

    app = FastAPI(title="Passive Operator — Build 3", docs_url=None, redoc_url=None,
                  openapi_url=None, redirect_slashes=False, lifespan=lifespan)
    app.state.switchboard = switchboard
    app.state.media_capture = media_capture
    app.state.transcription = transcription
    app.state.live_detection = live_detection
    app.state.detection_store = detection_store
    app.state.workspace_store = workspace_store
    app.state.agent_registry = agent_registry
    app.state.bridge_pipeline = bridge_pipeline
    app.state.detection_backfill = detection_backfill
    app.state.detection_writes = detection_writes
    app.state.voicemails = voicemails
    app.state.recordings = recordings
    app.state.call_details = call_details
    app.state.summaries = summaries
    register_dashboard(app, settings, transcription, voicemail_store=voicemails,
                       recording_library=recordings, call_details_store=call_details,
                       detection_store=detection_store, workspace_store=workspace_store)
    register_agent_routes(app, settings, agent_registry,
                          voice_settings=agent_voice or operator_voice, provider=agent_voice_provider)

    def require_owner(request):
        if request.method in {"GET", "HEAD"}:
            if not settings.agent_management_enabled or not owner_authenticated(request, agent_registry):
                raise HTTPException(403, "Unlock owner controls before using call controls.")
        else:
            owner_write_access(request, agent_registry, settings)
        return True
    # ``main`` passes the voice layer's settings when the operator may speak;
    # without them the keypad still parses and the bridge stays human relay.
    controller = register_operator_routes(app, settings, operator, dialer=operator_dialer,
        voice=operator_voice, registry=agent_registry if settings.agent_management_enabled else None,
        context_getter=bridge_pipeline.context, on_call_start=bridge_pipeline.start,
        on_call_end=bridge_pipeline.end, on_audio=bridge_pipeline.audio,
        on_output_audio=bridge_pipeline.output, on_agent_turn=bridge_pipeline.agent_turn,
        require_owner=require_owner, voicemail_store=voicemails)
    bridge_pipeline.controller = controller
    validate_twilio = twilio_validator(settings)

    @app.get("/")
    async def home():
        return RedirectResponse("/dashboard#calls/recent", status_code=307,
                                headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health():
        result = {"status": "ok", "service": "passive-operator", "build": 3,
                  "switchboard_ready": settings.switchboard_ready,
                  "media_capture_enabled": settings.media_capture_enabled and settings.switchboard_ready,
                  "transcription_enabled": settings.transcription_enabled and settings.switchboard_ready,
                  "voicemail_enabled": settings.voicemail_enabled and settings.switchboard_ready,
                  "operator_enabled": settings.operator_ready}
        if settings.deploy_commit:
            result["commit"] = settings.deploy_commit
        if settings.agent_management_enabled:
            result["agent_management_enabled"] = True
            result["manual_takeover_enabled"] = bool(settings.voice_agent_enabled and operator_voice)
            result["inbound_operator_enabled"] = settings.operator_inbound_enabled
            result["automatic_takeover_enabled"] = bool(settings.automatic_takeover_enabled and operator_voice)
            result["voicemail_agent_enabled"] = bool(settings.voicemail_agent_enabled and operator_voice)
            if result["voicemail_agent_enabled"]:
                result["voicemail_agent_ring_seconds"] = settings.voicemail_agent_ring_seconds
        if summaries.enabled:
            result["summaries_enabled"] = True
        if live_detection.enabled:
            result["detection_enabled"] = settings.switchboard_ready
        if detection_backfill.enabled:
            result["detection_backfill_enabled"] = settings.switchboard_ready
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
                             "active_sessions": switchboard.active_count + operator.active_count,
                             "pending_work": switchboard.pending_count + media_capture.active_count
                                             + media_capture.pending_count + transcription.active_count
                                             + voicemails.active_count + summaries.active_count
                                             + operator.pending_count + live_detection.active_count
                                             + detection_backfill.active_count
                                             + len(detection_writes) + bridge_pipeline.pending_count},
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
        await operator.set_draining(payload["draining"])
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
        accepted_repositories = {settings.deploy_repository}
        project_names = {"jimmmmmmmmmmmy/fictional-rotary-phone", "jimmmmmmmmmmmy/Phoney"}
        if settings.deploy_repository in project_names:
            accepted_repositories.update(project_names)
        if not isinstance(repository, dict) or repository.get("full_name") not in accepted_repositories:
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
        if settings.operator_inbound_enabled:
            if form.get("To") != settings.twilio_number:
                raise HTTPException(400, "Unexpected destination")
            try:
                session = await controller.start_inbound(
                    call_sid, str(form.get("From", "")), str(form.get("CallToken", "")))
            except OperatorRejected:
                # A rejected reservation must not create a second call through
                # the conference path or bypass the bridge's capacity/drain gate.
                response.say("The team is unavailable right now. Please try again later.")
                response.hangup()
                return Response(str(response), media_type="application/xml")
            return Response(controller.inbound_twiml(session), media_type="application/xml")
        if settings.switchboard_ready:
            if form.get("To") != settings.twilio_number:
                raise HTTPException(400, "Unexpected destination")
            try:
                session = await switchboard.start(
                    call_sid, str(form.get("From", "")), str(form.get("CallToken", "")))
            except SessionRejected:
                response.say("The team is unavailable right now. Please try again later.")
                response.hangup()
                return Response(str(response), media_type="application/xml")
            await asyncio.to_thread(call_details.start, call_sid, str(form.get("From", "")))
            if session.phase == "ended":
                response.hangup()
                return Response(str(response), media_type="application/xml")
            if session.phase == "voicemail":
                response.redirect(settings.public_base_url + f"/voicemail/{call_sid}", method="POST")
                return Response(str(response), media_type="application/xml")
            response.say("New College Data Science Team", language="en-US")
            if settings.media_capture_enabled:
                try:
                    ticket = media_capture.reserve(call_sid)
                except (ValueError, RuntimeError, OSError) as exc:
                    # Passive capture must not prevent the two humans talking.
                    logger.error("capture_reservation_failed type=%s", type(exc).__name__)
                else:
                    notice = ("This demo call records and transcribes audio for testing."
                              if settings.transcription_enabled else "This demo call records audio for testing.")
                    if settings.modulate_detection_enabled:
                        notice = ("This demo call records, transcribes, and analyzes audio for testing."
                                  if settings.transcription_enabled
                                  else "This demo call records and analyzes audio for testing.")
                    response.say(notice, language="en-US")
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
        session = switchboard.sessions.get(parent_call_sid)
        if session and session.phase == "voicemail":
            # A late Dial action can arrive while the REST redirect is in flight.
            # Returning Hangup here would cut off the new voicemail flow.
            await switchboard.finished(parent_call_sid)
            response = VoiceResponse()
            response.redirect(settings.public_base_url + f"/voicemail/{parent_call_sid}", method="POST")
            return Response(str(response), media_type="application/xml")
        await switchboard.finished(parent_call_sid)
        response = VoiceResponse()
        response.hangup()
        return Response(str(response), media_type="application/xml")

    @app.post("/voicemail/{parent_call_sid}")
    async def voicemail_start(parent_call_sid: str, form=Depends(validate_twilio)):
        require_sid(parent_call_sid)
        if require_sid(form.get("CallSid")) != parent_call_sid:
            raise HTTPException(400, "Voicemail callback does not match the call")
        response = VoiceResponse()
        if not await switchboard.voicemail_started(parent_call_sid):
            response.hangup()
            return Response(str(response), media_type="application/xml")
        session = switchboard.sessions[parent_call_sid]
        if not voicemails.start(parent_call_sid, session.voicemail_reason):
            await switchboard.voicemail_finished(parent_call_sid, "voicemail_unavailable")
            response.hangup()
            return Response(str(response), media_type="application/xml")
        notice = "recorded and transcribed" if settings.transcription_enabled else "recorded"
        response.say(
            "You've reached the New College Data Science Team. No one is available to answer. "
            f"Your message will be {notice}. After the beep, please leave your name, "
            "callback number, and message. Press pound when you are finished.", language="en-US")
        response.record(
            action=settings.public_base_url + f"/voicemail/finished/{parent_call_sid}",
            method="POST", max_length=settings.voicemail_max_seconds, timeout=5,
            finish_on_key="#", play_beep=True, trim="do-not-trim", transcribe=False,
            recording_status_callback=settings.public_base_url + f"/voicemail/recording/{parent_call_sid}",
            recording_status_callback_method="POST",
            recording_status_callback_event="in-progress completed absent",
        )
        return Response(str(response), media_type="application/xml")

    @app.post("/voicemail/finished/{parent_call_sid}")
    async def voicemail_finished(parent_call_sid: str, form=Depends(validate_twilio)):
        require_sid(parent_call_sid)
        if require_sid(form.get("CallSid")) != parent_call_sid:
            raise HTTPException(400, "Voicemail callback does not match the call")
        # The Record action is not evidence that Twilio has a downloadable file.
        # Only recordingStatusCallback can promote its receipt to completed.
        voicemails.finish(parent_call_sid)
        await switchboard.voicemail_finished(parent_call_sid)
        response = VoiceResponse()
        response.hangup()
        return Response(str(response), media_type="application/xml")

    @app.post("/voicemail/recording/{parent_call_sid}")
    async def voicemail_recording(parent_call_sid: str, form=Depends(validate_twilio)):
        require_sid(parent_call_sid)
        if require_sid(form.get("CallSid")) != parent_call_sid:
            raise HTTPException(400, "Recording callback does not match the call")
        recording_sid = require_sid(form.get("RecordingSid"), "RE")
        if form.get("RecordingSource", "RecordVerb") != "RecordVerb":
            raise HTTPException(400, "Unexpected recording source")
        status = str(form.get("RecordingStatus", ""))
        raw_duration = str(form.get("RecordingDuration", ""))
        if raw_duration and (not raw_duration.isascii() or not raw_duration.isdecimal() or len(raw_duration) > 4):
            raise HTTPException(400, "Invalid recording duration")
        duration = int(raw_duration) if raw_duration else None
        await voicemails.restore(parent_call_sid)
        if not voicemails.recording(parent_call_sid, recording_sid, status, duration):
            raise HTTPException(400, "Unknown or mismatched voicemail recording")
        if status in {"completed", "absent", "failed"}:
            reason = {"completed": "voicemail_recorded", "absent": "voicemail_absent", "failed": "voicemail_failed"}[status]
            await switchboard.voicemail_finished(parent_call_sid, reason)
        return Response(status_code=204)

    @app.post("/status")
    async def status(form=Depends(validate_twilio)):
        call_sid = require_sid(form.get("CallSid"))
        call_status = str(form.get("CallStatus", "unknown"))
        if call_status not in {"queued", "initiated", "ringing", "in-progress", "completed",
                               "busy", "failed", "no-answer", "canceled"}:
            call_status = "unknown"
        if call_status in {"completed", "busy", "failed", "no-answer", "canceled"}:
            raw_duration = str(form.get("CallDuration", ""))
            duration = (int(raw_duration) if raw_duration.isascii() and raw_duration.isdecimal()
                        and len(raw_duration) <= 6 else None)
            inbound = next((item for item in operator.sessions.values()
                            if item.direction == "inbound" and item.canonical_call_sid == call_sid), None)
            if inbound is not None:
                result = await operator.record_status(inbound.id, "remote", call_sid,
                                                      call_status, duration=duration)
                if result["action"] == "terminal":
                    await controller.end(inbound.id, "remote-" + result["reason"])
                await asyncio.to_thread(call_details.finish, call_sid, duration_seconds=duration)
                return Response(status_code=204)
            session = switchboard.sessions.get(form["CallSid"])
            if session and session.phase == "voicemail":
                await switchboard.voicemail_finished(form["CallSid"], "voicemail_hangup")
            else:
                await switchboard.finished(form["CallSid"])
            # The signed parent callback supplies the full call duration, including prompts.
            await asyncio.to_thread(call_details.finish, form["CallSid"], duration_seconds=duration)
        logger.info("call_status call_sid=%s status=%s", form["CallSid"], call_status)
        return Response(status_code=204)

    return app
