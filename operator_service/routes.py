"""Authenticated call-start API, leg TwiML, and the operator controller.

Routes stay thin: each one validates a Twilio signature or the admin bearer
token and then delegates to ``OperatorSessions`` (state) and ``CallRouter``
(audio). The controller is the only place that dials, and every network call it
starts is a tracked task so deployment draining can wait for it.

The controller also owns the keypad: ``controls`` decides what a keypress means,
and the controller applies it under the session's reply epoch. A takeover speaks
one cached phrase in the owner's voice, and ``#0`` returns control without
asking any provider for anything.

The owner-first callback needs no conference: each phone gets its own inline
``<Connect><Stream>`` and Python joins the two audio directions.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import threading

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

from voice_stack.audio import frame_count, iter_frames

from webhooks import require_sid, twilio_validator, valid_media_signature

from .audio import FRAME_SECONDS, CallRouter
from .controls import (DEFAULT_PROFILE, DIGITS, HASH, IGNORED, PREPARE_SECONDS, PROFILE,
                       RELEASE, REPORTED, ClipLibrary, Command, Keypad, load_profiles)
from .sessions import (AGENT, CONNECTED, HUMAN, MODES, OWNER, OWNER_PROMPT, REMOTE, ROLES,
                       OperatorRejected, OperatorSession, OperatorSessions)

log = logging.getLogger("uvicorn.error")

# Call states after which Twilio will not send another callback for that leg.
TERMINAL_STATUSES = frozenset({"completed", "busy", "no-answer", "failed", "canceled"})

# Twilio's per-leg ringing timeout. The store's owner-ringing guard adds a
# buffer for the callback that reports the timeout.
RING_TIMEOUT_SECONDS = 25
ACCEPT_DIGIT = "1"
ACCEPT_PROMPT = "Press 1 to connect."
# The clip player offers frames just ahead of the writer's 20 ms clock, so a long
# phrase neither overflows the one-second agent buffer nor gets cut mid-word.
CLIP_LEAD_FRAMES = 4
CLIP_STALL_SECONDS = 2.0


def leg_twiml(public_base: str, session_id: str, role: str, generation: int, token: str,
              *, digits: str | None = None, prompt: str | None = None) -> str:
    """Inline stream TwiML for one leg, with a recovery redirect when it ends.

    Media Streams URLs cannot carry a query string, so the session, role,
    generation, and one-use token travel in the path and in stream parameters.
    ``digits`` is reserved for the remote IVR workaround; a reconnect never
    repeats it.
    """
    response = VoiceResponse()
    if prompt:
        response.say(prompt, language="en-US")
    if digits is not None:
        response.play(digits=digits)
    stream = response.connect().stream(
        url=public_base.replace("https://", "wss://", 1) + f"/media/{session_id}/{role}/")
    stream.parameter(name="generation", value=str(generation))
    stream.parameter(name="token", value=token)
    response.redirect(public_base + f"/twilio/reconnect/{session_id}/{role}", method="POST")
    return str(response)


class TwilioLegs:
    """Bounded async facade over the blocking Twilio REST SDK for call legs.

    Construction is side-effect free. Only ``create_leg`` places a call, and it
    runs in a worker thread with a ten-second client timeout and no SDK retries:
    a retry could place a second call to a destination that already rang.
    """

    def __init__(self, settings):
        self.settings = settings
        # Requests sessions are not shared between concurrent executor threads.
        self._local = threading.local()

    def _client(self):
        if not hasattr(self._local, "client"):
            key = self.settings.api_key or self.settings.account_sid
            secret = self.settings.api_secret or self.settings.auth_token
            self._local.client = Client(
                key, secret, account_sid=self.settings.account_sid,
                http_client=TwilioHttpClient(timeout=10, max_retries=0),
            )
        return self._local.client

    async def create_leg(self, *, to: str, twiml: str, status_callback: str) -> str:
        def create():
            return self._client().calls.create(
                to=to,
                from_=self.settings.twilio_number,
                twiml=twiml,
                timeout=RING_TIMEOUT_SECONDS,
                time_limit=int(self.settings.max_call_seconds),
                status_callback=status_callback,
                status_callback_method="POST",
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            ).sid

        return await asyncio.to_thread(create)

    async def end_call(self, call_sid: str):
        def end():
            call = self._client().calls(call_sid)
            try:
                status = call.fetch().status
                if status in TERMINAL_STATUSES:
                    return
                if status in {"queued", "initiated", "ringing"}:
                    try:
                        call.update(status="canceled")
                        return
                    except TwilioRestException as exc:
                        # The callee can answer between fetch and cancel; only a
                        # call-state error warrants the alternate terminal update.
                        if exc.code != 21220:
                            raise
                call.update(status="completed")
            except TwilioRestException as exc:
                if exc.status != 404:
                    raise

        await asyncio.to_thread(end)


class OperatorController:
    """Owns the Twilio legs, the per-session router, and the phase deadlines."""

    def __init__(self, settings, store: OperatorSessions, dialer=None, *, voice_id="",
                 voice=None, profiles=None, keypad_factory=Keypad):
        self.settings = settings
        self.store = store
        self.dialer = dialer if dialer is not None else TwilioLegs(settings)
        self.voice = voice
        self.voice_id = voice_id or (voice.elevenlabs_voice_id if voice is not None else "")
        self.profiles = load_profiles() if profiles is None else dict(profiles)
        self.clips = ClipLibrary(voice) if voice is not None else None
        self.keypad_factory = keypad_factory
        self.keypads: dict[str, Keypad] = {}
        self.keys: dict[str, asyncio.Task] = {}        # pending keypad deadlines
        self.players: dict[str, asyncio.Task] = {}     # the phrase being spoken
        self.prefetch: dict[str, asyncio.Task] = {}    # the default phrase warming up
        self.routers: dict[str, CallRouter] = {}
        # Stage 3 installs a listener here; a sink must be synchronous and
        # bounded so a reader never waits on a provider.
        self.audio_sink = None
        # Stage 4 installs the "play digits on the existing remote call" path
        # here; nothing in this stage sends a remote IVR digit.
        self.digit_sender = None
        store.on_timeout = self.on_timeout
        store.on_end = self._on_session_end

    # ------------------------------------------------------------------ routing

    def router(self, session_id: str) -> CallRouter:
        router = self.routers.get(session_id)
        if router is None:
            router = CallRouter(session_id, self)
            self.routers[session_id] = router
        return router

    def on_audio(self, session_id: str, role: str, frame: bytes):
        if self.audio_sink is not None:
            self.audio_sink(session_id, role, frame)

    async def mark(self, session_id: str, role: str, name: str, state: str):
        """Record a playback mark; a mark returned after ``clear`` proves nothing.

        Twilio replays a queued mark even when the audio before it was cleared,
        so an interrupted phrase is never recorded as fully heard.
        """
        recorded = state
        if state == "played" and self.store.mark_state(session_id, role, name) == "cleared":
            recorded = "played-after-clear"
        self.store.note_mark(session_id, role, name, recorded)

    # ----------------------------------------------------------------- call flow

    async def start_outbound(self, to, goal, idempotency_key):
        """Reserve the session, let the API answer, then dial in a tracked task."""
        session, reused = await self.store.reserve_outbound(
            to, goal, idempotency_key, voice_id=self.voice_id)
        if not reused:
            self.store.spawn(self._dial_owner(session.id))
        return session, reused

    async def _dial_owner(self, session_id: str):
        if not await self.store.begin_owner_dial(session_id):
            return
        session = self.store.sessions.get(session_id)
        if session is None:
            return
        leg = session.legs[OWNER]
        twiml = leg_twiml(self.settings.public_base_url, session_id, OWNER,
                          leg.generation, leg.token, prompt=ACCEPT_PROMPT)
        await self._dial(session_id, OWNER, leg.destination, twiml)

    async def _dial_remote(self, session_id: str):
        session = self.store.sessions.get(session_id)
        if session is None:
            return
        leg = session.legs[REMOTE]
        twiml = leg_twiml(self.settings.public_base_url, session_id, REMOTE,
                          leg.generation, leg.token)
        await self._dial(session_id, REMOTE, leg.destination, twiml)

    async def _dial(self, session_id: str, role: str, destination: str, twiml: str):
        session = self.store.sessions.get(session_id)
        if session is None or not session.active:
            return
        try:
            call_sid = await self.dialer.create_leg(
                to=destination, twiml=twiml,
                status_callback=self.settings.public_base_url
                + f"/twilio/status/{session_id}/{role}")
            await self.store.bind_call_sid(session_id, role, call_sid)
        except OperatorRejected as exc:
            log.warning("operator_dial_rejected session=%s role=%s reason=%s",
                        session_id, role, exc.reason)
            await self.end(session_id, f"{role}-dial-mismatch")
        except Exception as exc:
            # A timeout can hide a successful dial, so reconcile through the
            # status callback before ending the session. Never dial again.
            log.warning("operator_dial_failed session=%s role=%s type=%s",
                        session_id, role, type(exc).__name__)
            await self.store.dial_failed(session_id, role)

    async def stream_started(self, session_id: str, role: str, stream_sid: str):
        if role == OWNER:
            await self.store.mark_owner_prompt(session_id)
            return
        if await self.store.mark_connected(session_id):
            self.router(session_id).stop_cue()
            self._prefetch_clip(session_id)
            log.info("operator_connected session=%s", session_id)

    async def stream_stopped(self, session_id: str, role: str, reason: str):
        # A stream ending is not a hangup: Twilio may still be running the
        # recovery redirect, and the call ends from its signed status callback.
        log.info("operator_stream_stopped session=%s role=%s reason=%s",
                 session_id, role, reason)

    async def dtmf(self, session_id: str, digit: str):
        session = self.store.sessions.get(session_id)
        if session is None or not session.active:
            return
        if session.phase == OWNER_PROMPT:
            if digit != ACCEPT_DIGIT:
                return          # Before acceptance only a bare 1 is meaningful.
            if not await self.store.begin_remote_dial(session_id):
                return
            self.router(session_id).start_cue(OWNER)
            self.store.spawn(self._dial_remote(session_id))
            return
        if session.phase != CONNECTED:
            # Keys before both legs are joined cannot select a profile: there is
            # nobody to delegate to yet, so they are consumed locally.
            log.info("operator_dtmf_early digit=%s phase=%s", digit, session.phase)
            return
        command = self._keypad(session_id).feed(digit, mode=session.mode)
        await self._run_command(session_id, command)
        self._arm_keypad(session_id)

    # ------------------------------------------------------------------- keypad

    def _keypad(self, session_id: str) -> Keypad:
        keypad = self.keypads.get(session_id)
        if keypad is None:
            keypad = self.keypad_factory()
            self.keypads[session_id] = keypad
        return keypad

    def _arm_keypad(self, session_id: str):
        """Give an unfinished prefix or digit run exactly one deadline."""
        session = self.store.sessions.get(session_id)
        keypad = self.keypads.get(session_id)
        task = self.keys.pop(session_id, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if session is None or not session.active or keypad is None:
            return
        remaining = keypad.remaining()
        if remaining is None:
            return
        self.keys[session_id] = asyncio.create_task(self._keypad_due(session_id, remaining))

    async def _keypad_due(self, session_id: str, delay: float):
        """Resolve a timeout-driven decision: an expired prefix or a digit run."""
        try:
            await asyncio.sleep(delay)
            keypad = self.keypads.get(session_id)
            command = keypad.expire() if keypad is not None else None
            if command is not None:
                await self._run_command(session_id, command)
                self._arm_keypad(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("operator_keypad_timer_failed type=%s", type(exc).__name__)
        finally:
            if self.keys.get(session_id) is asyncio.current_task():
                self.keys.pop(session_id, None)

    async def _run_command(self, session_id: str, command: Command):
        if command.kind == PROFILE:
            await self._take_over(session_id, command.value)
        elif command.kind == RELEASE:
            await self._release(session_id)
        elif command.kind in (DIGITS, HASH):
            await self._queue_ivr(session_id, command.value)
        elif command.reason in REPORTED:
            log.info("operator_keypad_ignored session=%s reason=%s",
                     session_id, command.reason)

    async def _take_over(self, session_id: str, key: str):
        """Select one saved profile and speak its fixed phrase in the owner's voice."""
        profile = self.profiles.get(key)
        if profile is None:
            log.error("operator_profile_missing session=%s profile=%s", session_id, key)
            return
        if self.clips is None or not profile.speaks:
            # No enrolled voice yet: keep the humans talking rather than hand the
            # remote leg to silence.
            log.warning("operator_takeover_unavailable session=%s profile=%s", session_id, key)
            return
        try:
            changed = await self.store.select_profile(session_id, key)
        except OperatorRejected as exc:
            log.warning("operator_takeover_rejected session=%s reason=%s", session_id, exc.reason)
            return
        if not changed:
            log.info("operator_profile_unchanged session=%s profile=%s", session_id, key)
            return
        session = self.store.sessions.get(session_id)
        if session is None:
            return
        self._stop_playback(session_id)
        router = self.router(session_id)
        result = router.set_mode(AGENT)
        if result is None:
            # Already delegated: a new profile still cancels the previous reply.
            result = router.clear(REMOTE)
        self._note_cleared(session_id, result)
        self._note_interrupted(session_id)
        epoch = session.reply_epoch
        self.players[session_id] = asyncio.create_task(self._speak(session_id, profile, epoch))
        log.info("operator_takeover session=%s profile=%s epoch=%d", session_id, key, epoch)

    async def _release(self, session_id: str):
        """``#0`` returns control immediately and never waits on a provider."""
        self._stop_playback(session_id)
        try:
            await self.store.set_mode(session_id, HUMAN)
        except OperatorRejected as exc:
            log.warning("operator_release_rejected session=%s reason=%s", session_id, exc.reason)
            return
        result = self.router(session_id).set_mode(HUMAN)
        if result is None:
            # Already human: nothing is buffered for the remote, but a phrase
            # that was still starting has just been invalidated by the epoch.
            result = self.router(session_id).clear(REMOTE)
        self._note_cleared(session_id, result)
        self._note_interrupted(session_id)
        log.info("operator_release session=%s", session_id)

    async def _queue_ivr(self, session_id: str, digits: str):
        """Hand remote IVR digits to the sender, or record that none is installed.

        The digit count is logged, never the digits: a menu answer can be an
        account number, and the keypad is the only place they arrived from.
        """
        if self.digit_sender is None:
            log.info("operator_ivr_digits_unmapped session=%s count=%d",
                     session_id, len(digits))
            return
        await self.digit_sender(session_id, digits)

    # -------------------------------------------------------------- agent audio

    async def _speak(self, session_id: str, profile, epoch: int):
        """Render a profile's phrase, then feed it frame by frame on the send clock.

        The reply epoch is re-checked before every frame, so a ``#0`` or a new
        profile stops the phrase where it is instead of letting a queued tail
        reach the remote leg after control changed hands.
        """
        try:
            async with asyncio.timeout(PREPARE_SECONDS):
                audio = await self.clips.bytes_for(profile.key, profile.demo_phrase)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("operator_clip_failed session=%s profile=%s type=%s",
                        session_id, profile.key, type(exc).__name__)
            await self._abandon(session_id, "clip-unavailable")
            return
        router = self.router(session_id)
        total = frame_count(audio)
        sent = 0
        for frame in iter_frames(audio):
            if not self._current(session_id, epoch):
                return
            stalled = 0.0
            while router.pending_agent(REMOTE) >= CLIP_LEAD_FRAMES:
                if not self._current(session_id, epoch):
                    return
                if stalled >= CLIP_STALL_SECONDS:
                    # Cancel a stuck utterance instead of replaying it late.
                    log.warning("operator_clip_stalled session=%s profile=%s",
                                session_id, profile.key)
                    return
                await asyncio.sleep(FRAME_SECONDS)
                stalled += FRAME_SECONDS
            if router.send_agent((frame,)) == 0:
                break       # The remote leg has no stream to hear it on.
            sent += 1
        if not sent:
            await self._abandon(session_id, "remote-unavailable")
            return
        if sent == total:
            # A mark only confirms playback for audio that was actually sent.
            await router.mark(REMOTE, f"clip-{profile.key}-{epoch}")
            log.info("operator_clip_spoken session=%s profile=%s frames=%d",
                     session_id, profile.key, sent)
        else:
            log.info("operator_clip_interrupted session=%s profile=%s frames=%d of %d",
                     session_id, profile.key, sent, total)

    async def _abandon(self, session_id: str, reason: str):
        """Hand control back when a takeover cannot produce audible speech."""
        log.warning("operator_takeover_abandoned session=%s reason=%s", session_id, reason)
        try:
            await self.store.set_mode(session_id, HUMAN)
        except OperatorRejected:
            return
        self._note_cleared(session_id, self.router(session_id).set_mode(HUMAN))

    def _prefetch_clip(self, session_id: str):
        """Render the default phrase while the two humans are still talking.

        The first ``#1`` must not wait on a provider, so the cached phrase is
        produced as soon as both legs are joined. A failure here stays quiet:
        the press path reports it privately if the phrase is still unavailable.
        """
        profile = self.profiles.get(DEFAULT_PROFILE)
        if self.clips is None or profile is None or not profile.speaks:
            return
        if self.clips.cached(profile.key, profile.demo_phrase):
            return
        task = self.prefetch.get(session_id)
        if task is not None and not task.done():
            return
        self.prefetch[session_id] = asyncio.create_task(self._warm(session_id, profile))

    async def _warm(self, session_id: str, profile):
        try:
            await self.clips.bytes_for(profile.key, profile.demo_phrase)
            log.info("operator_clip_ready session=%s profile=%s", session_id, profile.key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("operator_clip_prefetch_failed session=%s type=%s",
                        session_id, type(exc).__name__)
        finally:
            if self.prefetch.get(session_id) is asyncio.current_task():
                self.prefetch.pop(session_id, None)

    def _current(self, session_id: str, epoch: int) -> bool:
        """Whether this reply is still the one the owner asked to hear."""
        session = self.store.sessions.get(session_id)
        return bool(session is not None and session.active
                    and session.mode == AGENT and session.reply_epoch == epoch)

    def playing(self, session_id: str) -> bool:
        """Whether a phrase is still being spoken for this session."""
        task = self.players.get(session_id)
        return bool(task is not None and not task.done())

    def _stop_playback(self, session_id: str):
        """Cancel the phrase being spoken; the epoch change already invalidates it."""
        task = self.players.pop(session_id, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _note_cleared(self, session_id: str, result):
        """Record the phrases a clear invalidated: an interrupted phrase was heard."""
        for role, name in (result or {}).get("marks", ()):
            self.store.note_mark(session_id, role, name, "cleared")

    def _note_interrupted(self, session_id: str):
        """A clear also interrupts a phrase whose mark was already sent.

        Twilio returns a mark when its playback finishes, so a phrase that was
        sent but has not been confirmed yet may have been cut off by this clear.
        Recording it as interrupted is what stops a late ``played`` from being
        mistaken for a phrase the remote party actually heard.
        """
        session = self.store.sessions.get(session_id)
        leg = session.legs.get(REMOTE) if session is not None else None
        if leg is None:
            return
        for name, state in list(leg.marks.items()):
            if state == "pending":
                self.store.note_mark(session_id, REMOTE, name, "cleared")

    def _stop_session_tasks(self, session_id: str):
        """Drop a session's keypad state and cancel its playback work."""
        for table in (self.keys, self.prefetch):
            task = table.pop(session_id, None)
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
        self._stop_playback(session_id)
        self.keypads.pop(session_id, None)

    async def _on_session_end(self, session_id: str):
        self._stop_session_tasks(session_id)

    async def set_mode(self, session_id: str, mode: str):
        if mode not in MODES:
            raise OperatorRejected("invalid-mode")
        if mode == HUMAN:
            # An admin mode change interrupts speech just like ``#0`` does.
            self._stop_playback(session_id)
        router = self.router(session_id)
        if not await self.store.set_mode(session_id, mode):
            return False
        result = router.set_mode(mode)
        self._note_cleared(session_id, result)
        self._note_interrupted(session_id)
        return True

    async def on_timeout(self, session: OperatorSession, reason: str):
        await self.end(session.id, reason)

    async def end(self, session_id: str, reason: str = "admin-end"):
        session = self.store.find(session_id)
        if session is None:
            raise OperatorRejected("unknown-session")
        router = self.routers.pop(session_id, None)
        if router is not None:
            router.close()
        for role, call_sid in await self.store.end(session_id, reason):
            self.store.spawn(self._safe_end_call(role, call_sid))
        return session

    async def _safe_end_call(self, role: str, call_sid: str):
        try:
            await self.dialer.end_call(call_sid)
        except Exception as exc:
            log.warning("operator_end_call_failed role=%s type=%s", role, type(exc).__name__)


def _status_for(reason: str) -> int:
    if reason == "capacity":
        return 429
    if reason == "destination-not-allowed":
        return 403
    if reason in {"draining", "shutting-down", "not-configured"}:
        return 503
    return 400


def _xml(response) -> Response:
    return Response(str(response), media_type="application/xml")


def register_operator_routes(app: FastAPI, settings, store: OperatorSessions,
                             dialer=None, voice=None) -> OperatorController:
    """Mount the operator bridge beside the existing conference path."""
    controller = OperatorController(settings, store, dialer, voice=voice)
    app.state.operator = store
    app.state.operator_controller = controller
    validate_twilio = twilio_validator(settings)

    async def require_admin(request: Request):
        token = str(getattr(settings, "operator_admin_token", ""))
        if not token or not hmac.compare_digest(
                request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()):
            raise HTTPException(403, "Invalid operator admin token")
        if not store.ready:
            raise HTTPException(503, "The operator bridge is not configured")

    @app.post("/api/calls/outbound", status_code=202, dependencies=[Depends(require_admin)])
    async def outbound(request: Request):
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected a JSON object") from None
        if (not isinstance(payload, dict) or "to" not in payload
                or set(payload) - {"to", "goal"} or not isinstance(payload["to"], str)
                or not isinstance(payload.get("goal", ""), str)):
            raise HTTPException(400, 'Expected {"to": "<allowlisted E.164>", "goal": "..."}')
        try:
            session, reused = await controller.start_outbound(
                payload["to"], payload.get("goal", ""), request.headers.get("idempotency-key", ""))
        except OperatorRejected as exc:
            raise HTTPException(_status_for(exc.reason),
                                f"Cannot start a call: {exc.reason}") from None
        return JSONResponse({"session_id": session.id, "phase": session.phase,
                             "duplicate": reused,
                             "status_url": f"/api/sessions/{session.id}"},
                            status_code=202, headers={"Cache-Control": "no-store"})

    @app.websocket("/media/{session_id}/{role}/")
    async def bridge_socket(websocket: WebSocket, session_id: str, role: str):
        session = store.find(session_id)
        if (role not in ROLES or session is None or not session.active
                or not valid_media_signature(settings, websocket)):
            log.warning("operator_handshake_rejected session=%s role=%s", session_id, role)
            await websocket.close(code=1008)
            return
        await controller.router(session_id).serve(websocket, role, store)

    @app.post("/twilio/status/{session_id}/{role}")
    async def leg_status(session_id: str, role: str, form=Depends(validate_twilio)):
        if role not in ROLES:
            raise HTTPException(400, "Unknown operator role")
        call_sid = require_sid(form.get("CallSid"))
        raw_duration = str(form.get("CallDuration", ""))
        duration = (int(raw_duration) if raw_duration.isascii() and raw_duration.isdecimal()
                    and len(raw_duration) <= 6 else None)
        try:
            result = await store.record_status(session_id, role, call_sid,
                                               form.get("CallStatus"), duration=duration)
        except OperatorRejected as exc:
            raise HTTPException(400, f"Call callback does not match: {exc.reason}") from None
        if result["action"] == "terminal":
            await controller.end(session_id, f"{role}-{result['reason']}")
            log.info("operator_call_ended session=%s role=%s reason=%s",
                     session_id, role, result["reason"])
        return Response(status_code=204)

    @app.post("/twilio/reconnect/{session_id}/{role}")
    async def leg_reconnect(session_id: str, role: str, form=Depends(validate_twilio)):
        if role not in ROLES:
            raise HTTPException(400, "Unknown operator role")
        call_sid = require_sid(form.get("CallSid"))
        session = store.find(session_id)
        if session is None or not session.active:
            response = VoiceResponse()
            response.hangup()
            return _xml(response)
        leg = session.legs[role]
        if leg.call_sid and leg.call_sid != call_sid:
            raise HTTPException(400, "Recovery callback does not match the leg")
        try:
            leg = await store.rotate_token(session_id, role)
        except OperatorRejected as exc:
            log.warning("operator_reconnect_rejected session=%s role=%s reason=%s",
                        session_id, role, exc.reason)
            await controller.end(session_id, f"{role}-{exc.reason}")
            response = VoiceResponse()
            response.hangup()
            return _xml(response)
        return _xml(leg_twiml(settings.public_base_url, session_id, role,
                              leg.generation, leg.token))

    @app.get("/api/sessions/{session_id}", dependencies=[Depends(require_admin)])
    async def session_status(session_id: str):
        session = store.find(session_id)
        if session is None:
            raise HTTPException(404, "Unknown session")
        return JSONResponse(session.to_status(), headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions/{session_id}/mode", dependencies=[Depends(require_admin)])
    async def session_mode(session_id: str, request: Request):
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected a JSON object") from None
        if (not isinstance(payload, dict) or set(payload) != {"mode"}
                or payload["mode"] not in MODES):
            raise HTTPException(400, 'Expected {"mode": "human"} or {"mode": "agent"}')
        try:
            changed = await controller.set_mode(session_id, payload["mode"])
        except OperatorRejected as exc:
            raise HTTPException(404 if exc.reason == "unknown-session" else 409,
                                f"Cannot change mode: {exc.reason}") from None
        return JSONResponse({"session_id": session_id, "mode": payload["mode"],
                             "changed": changed}, headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions/{session_id}/end", dependencies=[Depends(require_admin)])
    async def session_end(session_id: str):
        try:
            session = await controller.end(session_id, "admin-end")
        except OperatorRejected as exc:
            raise HTTPException(404, "Unknown session") from None
        return JSONResponse(session.to_status(), headers={"Cache-Control": "no-store"})

    return controller
