"""Authenticated call-start API, leg TwiML, and the operator controller.

Routes stay thin: each one validates a Twilio signature or the admin bearer
token and then delegates to ``OperatorSessions`` (state) and ``CallRouter``
(audio). The controller is the only place that dials, and every network call it
starts is a tracked task so deployment draining can wait for it.

The controller owns manual keypad and dashboard takeover requests. Published
agent snapshots drive cancellable dialogue; ``#0`` returns control without
asking any provider for anything. No detection result can activate an agent.

The owner-first callback needs no conference: each phone gets its own inline
``<Connect><Stream>`` and Python joins the two audio directions.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import threading
import time
import inspect

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

from webhooks import require_sid, twilio_validator, valid_media_signature

from .audio import CallRouter
from .controls import (DIGITS, HASH, PROFILE, RELEASE, REPORTED, Command, Keypad)
from .sessions import (AGENT, ANNOUNCING, PREPARING, CALL_SID, CONNECTED, HUMAN, MODES, OWNER, OWNER_PROMPT, REMOTE, ROLES,
                       OperatorRejected, OperatorSession, OperatorSessions)
from .runtime import DialogueRun, maybe_await

log = logging.getLogger("uvicorn.error")

# Call states after which Twilio will not send another callback for that leg.
TERMINAL_STATUSES = frozenset({"completed", "busy", "no-answer", "failed", "canceled"})

# Twilio's per-leg ringing timeout. The store's owner-ringing guard adds a
# buffer for the callback that reports the timeout.
RING_TIMEOUT_SECONDS = 25
ACCEPT_DIGIT = "1"
ACCEPT_PROMPT = "Press 1 to connect."
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
                 voice=None, keypad_factory=Keypad, registry=None,
                 context_getter=None, on_call_start=None, on_call_end=None,
                 on_audio=None, on_output_audio=None, on_agent_turn=None,
                 provider_transport=None):
        self.settings = settings
        self.store = store
        self.dialer = dialer if dialer is not None else TwilioLegs(settings)
        self.voice = voice
        self.registry = registry
        self.context_getter = context_getter
        self.call_start_callback, self.call_end_callback = on_call_start, on_call_end
        self.audio_callback, self.output_callback = on_audio, on_output_audio
        self.agent_turn_callback = on_agent_turn
        self.provider_transport = provider_transport
        self._started = set()
        self._mark_waiters = {}
        self._transcript_seen = {}
        self._reply_timers = {}
        self._takeover_requests = {}
        self._context_revisions = {}
        self._output_clocks = {}
        self._agent_frames_sent = {}
        self.voice_id = voice_id or (voice.elevenlabs_voice_id if voice is not None else "")
        self.keypad_factory = keypad_factory
        self.keypads: dict[str, Keypad] = {}
        self.keys: dict[str, asyncio.Task] = {}        # pending keypad deadlines
        self.players: dict[str, asyncio.Task] = {}     # the phrase being spoken
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

    def elapsed_ms(self, session_id):
        session = self.store.sessions.get(session_id)
        return max(0, int((time.monotonic() - session.created) * 1000)) if session else 0

    def relay_ready(self, session_id):
        session = self.store.sessions.get(session_id)
        return bool(session and session.active and session.phase == CONNECTED)

    @property
    def voice_ready(self):
        return bool(getattr(self.settings, "voice_agent_enabled", False)
                    and self.voice and self.voice.enabled and self.voice.gemini_api_key
                    and self.voice.elevenlabs_api_key and self.voice.twilio_ready and self.registry)

    async def _callback(self, callback, *args, _budget=3, **kwargs):
        if callback is None:
            return
        try:
            async with asyncio.timeout(_budget):
                await maybe_await(callback(*args, **kwargs))
        except Exception as exc:
            log.warning("operator_observer_failed type=%s", type(exc).__name__)

    def on_audio(self, session_id: str, role: str, frame: bytes, timestamp_ms=None):
        if self.audio_sink is not None:
            self.audio_sink(session_id, role, frame)
        session = self.store.sessions.get(session_id)
        if self.audio_callback is not None and session is not None:
            try:
                self.audio_callback(session, role, frame,
                    self.elapsed_ms(session_id) if timestamp_ms is None else timestamp_ms)
            except Exception as exc:
                log.warning("operator_audio_observer_failed type=%s", type(exc).__name__)

    def output_audio(self, session_id, frame, kind):
        session = self.store.sessions.get(session_id)
        if kind == "agent":
            self._agent_frames_sent[session_id] = self._agent_frames_sent.get(session_id, 0) + 1
        if self.output_callback is not None and session is not None:
            try:
                stamp = max(self.elapsed_ms(session_id), self._output_clocks.get(session_id, -20) + 20)
                self._output_clocks[session_id] = stamp
                self.output_callback(session, frame, stamp, kind)
            except Exception as exc:
                log.warning("operator_output_observer_failed type=%s", type(exc).__name__)

    async def agent_turn(self, session, text, start_ms, end_ms, **kwargs):
        await self._callback(self.agent_turn_callback, session, text, start_ms, end_ms, **kwargs)

    async def _notify_started(self, session):
        if session.canonical_call_sid and session.id not in self._started:
            self._started.add(session.id)
            await self._callback(self.call_start_callback, session)

    async def mark(self, session_id: str, role: str, name: str, state: str):
        """Record a playback mark; a mark returned after ``clear`` proves nothing.

        Twilio replays a queued mark even when the audio before it was cleared,
        so an interrupted phrase is never recorded as fully heard.
        """
        recorded = state
        if state == "played" and self.store.mark_state(session_id, role, name) == "cleared":
            recorded = "played-after-clear"
        self.store.note_mark(session_id, role, name, recorded)
        waiter = self._mark_waiters.get((session_id, name))
        if role == REMOTE and waiter is not None and not waiter.done():
            if recorded == "played":
                waiter.set_result(True)
            elif recorded != "pending":
                waiter.set_exception(OperatorRejected("playback-interrupted"))

    async def wait_for_mark(self, session_id, name, *, timeout=6.0):
        future = asyncio.get_running_loop().create_future()
        key = (session_id, name)
        self._mark_waiters[key] = future
        try:
            if not await self.router(session_id).mark(REMOTE, name):
                raise OperatorRejected("remote-unavailable")
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._mark_waiters.pop(key, None)
            if not future.done():
                future.cancel()

    # ----------------------------------------------------------------- call flow

    async def start_outbound(self, to, goal, idempotency_key):
        """Reserve the session, let the API answer, then dial in a tracked task."""
        session, reused = await self.store.reserve_outbound(
            to, goal, idempotency_key, voice_id=self.voice_id)
        if not reused:
            self.store.spawn(self._dial_owner(session.id))
        return session, reused

    async def start_inbound(self, call_sid, caller):
        session, reused = await self.store.reserve_inbound(call_sid, caller)
        if not reused:
            self.store.spawn(self._dial_owner(session.id))
        return session

    def inbound_twiml(self, session):
        leg = session.legs[REMOTE]
        notice = None
        if self.settings.media_capture_enabled:
            notice = ("This demo call records and transcribes audio for testing."
                      if self.settings.transcription_enabled else "This demo call records audio for testing.")
            if self.settings.modulate_detection_enabled:
                notice = ("This demo call records, transcribes, and analyzes audio for testing."
                          if self.settings.transcription_enabled
                          else "This demo call records and analyzes audio for testing.")
        return leg_twiml(self.settings.public_base_url, session.id, REMOTE,
                         leg.generation, leg.token, prompt=notice)

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
        call_sid = None
        try:
            call_sid = await self.dialer.create_leg(
                to=destination, twiml=twiml,
                status_callback=self.settings.public_base_url
                + f"/twilio/status/{session_id}/{role}")
            await self.store.bind_call_sid(session_id, role, call_sid)
        except OperatorRejected as exc:
            log.warning("operator_dial_rejected session=%s role=%s reason=%s",
                        session_id, role, exc.reason)
            if (isinstance(call_sid, str) and CALL_SID.fullmatch(call_sid)
                    and session.legs[role].call_sid != call_sid):
                # A terminal callback can win the race with calls.create. The
                # unbound REST result still names a live call we must end.
                # An already-bound SID is owned by the store's once-only cleanup.
                self.store.spawn(self._safe_end_call(role, call_sid))
            await self.end(session_id, f"{role}-dial-mismatch")
        except Exception as exc:
            # A timeout can hide a successful dial, so reconcile through the
            # status callback before ending the session. Never dial again.
            log.warning("operator_dial_failed session=%s role=%s type=%s",
                        session_id, role, type(exc).__name__)
            await self.store.dial_failed(session_id, role)

    async def stream_started(self, session_id: str, role: str, stream_sid: str):
        session = self.store.sessions.get(session_id)
        if session is None:
            return
        if role == OWNER:
            await self.store.mark_owner_prompt(session_id)
            return
        if not session.canonical_call_sid:
            session.canonical_call_sid = session.legs[REMOTE].call_sid
        await self._notify_started(session)
        if session.direction == "inbound" and session.phase != CONNECTED:
            self.router(session_id).start_cue(REMOTE)
            return
        if await self.store.mark_connected(session_id):
            self.router(session_id).stop_cue()
            log.info("operator_connected session=%s", session_id)

    async def stream_stopped(self, session_id: str, role: str, reason: str):
        # A stream ending is not a hangup: Twilio may still be running the
        # recovery redirect, and the call ends from its signed status callback.
        log.info("operator_stream_stopped session=%s role=%s reason=%s",
                 session_id, role, reason)
        session = self.store.sessions.get(session_id)
        if session is not None and session.mode != HUMAN:
            await self._release(session_id)

    async def dtmf(self, session_id: str, digit: str):
        session = self.store.sessions.get(session_id)
        if session is None or not session.active:
            return
        if session.phase == OWNER_PROMPT:
            if digit != ACCEPT_DIGIT:
                return          # Before acceptance only a bare 1 is meaningful.
            if session.direction == "inbound":
                if not self.router(session_id).attached(REMOTE):
                    return
                await self.store.mark_connected(session_id)
                self.router(session_id).stop_cue()
                return
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
            # A published-slot lookup may wait on SQLite. Keep that work off
            # the phone's media reader, including while its result is awaited.
            request_id = self._takeover_requests.get(session_id, 0) + 1
            self._takeover_requests[session_id] = request_id
            self.store.spawn(self._take_over(session_id, command.value, request_id=request_id))
        elif command.kind == RELEASE:
            await self._release(session_id)
        elif command.kind in (DIGITS, HASH):
            await self._queue_ivr(session_id, command.value)
        elif command.reason in REPORTED:
            log.info("operator_keypad_ignored session=%s reason=%s",
                     session_id, command.reason)

    async def _take_over(self, session_id: str, key: str, *, request_id=None):
        """Keypad commands share the same validation as dashboard commands."""
        try:
            return await self.takeover(session_id, key, request_id=request_id)
        except OperatorRejected as exc:
            log.info("operator_takeover_refused session=%s reason=%s", session_id, exc.reason)
            return False

    async def takeover(self, session_id, slot, *, request_id=None):
        session = self.store.find(session_id)
        if session is None or not session.active:
            raise OperatorRejected("unknown-session")
        if not self.voice_ready:
            raise OperatorRejected("voice-not-ready")
        if not isinstance(slot, str) or slot not in tuple(str(i) for i in range(1, 10)):
            raise OperatorRejected("invalid-slot")
        router = self.router(session_id)
        if session.phase != CONNECTED or not all(router.attached(role) for role in ROLES):
            raise OperatorRejected("call-not-connected")
        if request_id is None:
            request_id = self._takeover_requests.get(session_id, 0) + 1
            self._takeover_requests[session_id] = request_id
        elif self._takeover_requests.get(session_id) != request_id:
            raise OperatorRejected("takeover-canceled")
        resolver = self.registry.resolve_slot
        try:
            async with asyncio.timeout(3):
                snapshot = (await resolver(slot) if inspect.iscoroutinefunction(resolver)
                            else await asyncio.to_thread(resolver, slot))
        except Exception as exc:
            log.warning("operator_slot_lookup_failed type=%s", type(exc).__name__)
            raise OperatorRejected("slot-unavailable") from None
        if self._takeover_requests.get(session_id) != request_id:
            raise OperatorRejected("takeover-canceled")
        if snapshot is None:
            raise OperatorRejected("slot-not-ready")
        if self.context_getter is not None:
            try:
                async with asyncio.timeout(3):
                    await maybe_await(self.context_getter(session))
            except Exception as exc:
                log.warning("operator_context_unavailable type=%s", type(exc).__name__)
                raise OperatorRejected("transcription-unavailable") from None
            if self._takeover_requests.get(session_id) != request_id:
                raise OperatorRejected("takeover-canceled")
        changed = await self.store.select_profile(session_id, slot, mode=PREPARING)
        if not changed:
            return False
        self._stop_playback(session_id)
        timer = self._reply_timers.pop(session_id, None)
        if timer is not None:
            timer.cancel()
        self._note_cleared(session_id, router.set_mode(PREPARING))
        self._note_interrupted(session_id)
        session.agent_snapshot = snapshot
        session.agent_name = snapshot.name
        session.voice_id = snapshot.voice_id
        self._start_dialogue(session, announce=True)
        return True

    def _start_dialogue(self, session, *, announce=False):
        run = DialogueRun(self, session, session.agent_snapshot, session.reply_epoch,
                          announce=announce)
        task = self.store.spawn(run.run())
        self.players[session.id] = task
        def done(finished):
            if self.players.get(session.id) is finished:
                self.players.pop(session.id, None)
        task.add_done_callback(done)

    async def transcript(self, session_id, speaker, text, *, final=True,
                         segment_id="", timestamp_ms=None):
        """Receive canonical STT updates; only remote speech drives dialogue."""
        session = self.store.find(session_id)
        text = str(text or "").strip()
        if session is None or not session.active or not text or speaker not in (OWNER, REMOTE):
            return
        if final and segment_id:
            seen = self._transcript_seen.setdefault(session_id, {})
            if segment_id in seen:
                return
            seen[segment_id] = True
            while len(seen) > 1000:
                seen.pop(next(iter(seen)))
        if final:
            self._context_revisions[session_id] = self._context_revisions.get(session_id, 0) + 1
            await self.store.add_turn(session_id, speaker, text)
            if session.mode == PREPARING and session.agent_snapshot is not None:
                # A real turn arriving during preparation supersedes the old
                # answer while both humans continue hearing each other.
                await self.store.invalidate_reply(session_id)
                self._stop_playback(session_id)
                self._start_dialogue(session, announce=True)
                return
        if speaker == REMOTE and session.mode == AGENT:
            # Interim remote speech clears queued agent audio immediately.
            if self.playing(session_id):
                await self.store.invalidate_reply(session_id)
                self._stop_playback(session_id)
                self._note_cleared(session_id, self.router(session_id).clear(*ROLES))
                self._note_interrupted(session_id)
            timer = self._reply_timers.pop(session_id, None)
            if timer is not None:
                timer.cancel()
            if final and session.agent_snapshot is not None:
                self._reply_timers[session_id] = self.store.spawn(self._after_remote_turn(session_id))

    async def _after_remote_turn(self, session_id):
        try:
            await asyncio.sleep(0.3)
            session = self.store.find(session_id)
            if session is not None and session.active and session.mode == AGENT:
                await self.store.invalidate_reply(session_id)
                self._start_dialogue(session)
        finally:
            if self._reply_timers.get(session_id) is asyncio.current_task():
                self._reply_timers.pop(session_id, None)

    async def _release(self, session_id: str):
        """``#0`` returns control immediately and never waits on a provider."""
        self._takeover_requests[session_id] = self._takeover_requests.get(session_id, 0) + 1
        self._stop_playback(session_id)
        timer = self._reply_timers.pop(session_id, None)
        if timer is not None:
            timer.cancel()
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

    async def _abandon(self, session_id, reason):
        log.warning("operator_takeover_abandoned session=%s reason=%s", session_id, reason)
        await self._release(session_id)

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
        for table in (self.keys, self._reply_timers):
            task = table.pop(session_id, None)
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
        self._stop_playback(session_id)
        self.keypads.pop(session_id, None)
        self._transcript_seen.pop(session_id, None)
        self._takeover_requests.pop(session_id, None)
        self._context_revisions.pop(session_id, None)
        self._output_clocks.pop(session_id, None)

    async def _on_session_end(self, session_id: str):
        player = self.players.get(session_id)
        self._stop_session_tasks(session_id)
        router = self.routers.pop(session_id, None)
        if router is not None:
            router.close()
        if player is not None and player is not asyncio.current_task():
            await asyncio.gather(player, return_exceptions=True)
        self._agent_frames_sent.pop(session_id, None)
        session = self.store.find(session_id)
        if session is not None:
            await self._callback(self.call_end_callback, session, _budget=35)

    async def set_mode(self, session_id: str, mode: str, *, slot=None):
        if mode == AGENT:
            return await self.takeover(session_id, slot)
        if mode != HUMAN:
            raise OperatorRejected("invalid-mode")
        session = self.store.find(session_id)
        if session is None:
            raise OperatorRejected("unknown-session")
        if not session.active:
            raise OperatorRejected("session-ended")
        changed = session.mode != HUMAN
        await self._release(session_id)
        return changed

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
                             dialer=None, voice=None, *, registry=None, context_getter=None,
                             on_call_start=None, on_call_end=None, on_audio=None,
                             on_output_audio=None, on_agent_turn=None, require_owner=None,
                             provider_transport=None) -> OperatorController:
    """Mount the operator bridge beside the existing conference path."""
    controller = OperatorController(settings, store, dialer, voice=voice, registry=registry,
        context_getter=context_getter, on_call_start=on_call_start, on_call_end=on_call_end,
        on_audio=on_audio, on_output_audio=on_output_audio, on_agent_turn=on_agent_turn,
        provider_transport=provider_transport)
    app.state.operator = store
    app.state.operator_controller = controller
    validate_twilio = twilio_validator(settings)

    async def require_admin(request: Request):
        token = str(getattr(settings, "operator_admin_token", ""))
        valid_admin = bool(token and hmac.compare_digest(
                request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()))
        if not valid_admin:
            if require_owner is None or not getattr(settings, "agent_management_enabled", False):
                raise HTTPException(403, "Owner authentication required")
            authorized = (await require_owner(request) if inspect.iscoroutinefunction(require_owner)
                          else await asyncio.to_thread(require_owner, request))
            if authorized is False:
                raise HTTPException(403, "Owner authentication required")

    @app.get("/api/operator/sessions", dependencies=[Depends(require_admin)])
    async def active_sessions():
        return JSONResponse({"sessions": [session.to_status() for session in store.sessions.values()
            if session.active and session.phase == CONNECTED], "voice_ready": controller.voice_ready},
            headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions/{session_id}/takeover", dependencies=[Depends(require_admin)])
    async def session_takeover(session_id: str, request: Request):
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected a JSON object") from None
        if not isinstance(payload, dict) or set(payload) != {"slot"}:
            raise HTTPException(400, 'Expected {"slot": "1"} through {"slot": "9"}')
        try:
            changed = await controller.takeover(session_id, payload["slot"])
        except OperatorRejected as exc:
            raise HTTPException(404 if exc.reason == "unknown-session" else 409,
                                f"Cannot take over: {exc.reason}") from None
        session = store.find(session_id)
        return JSONResponse({"session_id": session_id, "mode": session.mode,
                             "slot": payload["slot"], "changed": changed},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/calls/outbound", status_code=202, dependencies=[Depends(require_admin)])
    async def outbound(request: Request):
        if not store.ready:
            raise HTTPException(503, "The operator bridge is not configured")
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
        if (not isinstance(payload, dict) or set(payload) not in ({"mode"}, {"mode", "slot"})
                or payload["mode"] not in (HUMAN, AGENT)
                or (payload["mode"] == AGENT and "slot" not in payload)
                or (payload["mode"] == HUMAN and "slot" in payload)):
            raise HTTPException(400, 'Use {"mode": "human"}, or {"mode": "agent", "slot": "1"}')
        try:
            changed = await controller.set_mode(session_id, payload["mode"], slot=payload.get("slot"))
        except OperatorRejected as exc:
            raise HTTPException(404 if exc.reason == "unknown-session" else 409,
                                f"Cannot change mode: {exc.reason}") from None
        return JSONResponse({"session_id": session_id, "mode": store.find(session_id).mode,
                             "changed": changed}, headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions/{session_id}/end", dependencies=[Depends(require_admin)])
    async def session_end(session_id: str):
        try:
            session = await controller.end(session_id, "admin-end")
        except OperatorRejected as exc:
            raise HTTPException(404, "Unknown session") from None
        return JSONResponse(session.to_status(), headers={"Cache-Control": "no-store"})

    return controller
