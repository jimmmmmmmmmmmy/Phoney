"""Authenticated call-start API, leg TwiML, and the operator controller.

Routes stay thin: each one validates a Twilio signature or the admin bearer
token and then delegates to ``OperatorSessions`` (state) and ``CallRouter``
(audio). The controller is the only place that dials, and every network call it
starts is a tracked task so deployment draining can wait for it.

The controller owns manual requests and explicitly enabled detection/voicemail
handoffs. Immutable agent snapshots drive cancellable dialogue; ``#0`` returns
control without a provider request and suppresses further automatic takeover.

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
import json
from contextlib import asynccontextmanager

import httpx
from agent_registry.auth import SAFE_HEADERS, owner_authenticated
from caller_id import forwarding_identity
from partner_detection.analysis import validate_analysis
from .internal_agents import internal_snapshot

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
from .runtime import DialogueRun, PREPARATION_SECONDS, maybe_await, voicemail_greeting_audio
from .voicemail_agent import VoicemailAgent

log = logging.getLogger("uvicorn.error")

# Call states after which Twilio will not send another callback for that leg.
TERMINAL_STATUSES = frozenset({"completed", "busy", "no-answer", "failed", "canceled"})

# Twilio's per-leg ringing timeout. The store's owner-ringing guard adds a
# buffer for the callback that reports the timeout.
RING_TIMEOUT_SECONDS = 25
DISCONNECT_STATUS_DELAYS = (5.0, 15.0, 30.0, 60.0)
STATUS_FETCH_SECONDS = 12.0
RECONNECT_PAUSE_SECONDS = 8
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
    # This is reached only after the media stream ends. Give a briefly lost
    # tunnel time to reconnect before asking it for fresh stream credentials.
    response.pause(length=RECONNECT_PAUSE_SECONDS)
    response.redirect(public_base + f"/twilio/reconnect/{session_id}/{role}"
                      + "#rc=2&rp=ct,5xx&tt=15000", method="POST")
    return str(response)


def voicemail_recording_twiml(settings, call_sid):
    """Provider-independent fallback on the existing caller leg."""
    response = VoiceResponse()
    response.say("Please leave your name, callback number, and message after the beep. "
                 "Press pound when you are finished.", language="en-US")
    response.record(action=settings.public_base_url + f"/twilio/voicemail/finished/{call_sid}",
        method="POST", max_length=settings.voicemail_max_seconds, timeout=5,
        finish_on_key="#", play_beep=True, trim="do-not-trim", transcribe=False,
        recording_status_callback=settings.public_base_url + f"/twilio/voicemail/recording/{call_sid}",
        recording_status_callback_method="POST",
        recording_status_callback_event="in-progress completed absent")
    response.hangup()
    return str(response)


def voicemail_closing_twiml():
    """Close after captured caller speech without asking for the message again."""
    response = VoiceResponse()
    response.say("Thank you for your message. I cannot read it back right now. Goodbye.",
                 language="en-US")
    response.hangup()
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

    async def create_leg(self, *, to: str, twiml: str, status_callback: str,
                         caller_number: str = "", call_token: str = "") -> str:
        def create():
            return self._client().calls.create(
                to=to,
                **forwarding_identity(self.settings.twilio_number, caller_number, call_token),
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

    async def replace_twiml(self, call_sid: str, twiml: str):
        """Replace the failed agent stream with native recording on the same call."""
        await asyncio.to_thread(lambda: self._client().calls(call_sid).update(twiml=twiml))

    async def recording_audio(self, recording_sid: str):
        """Fetch one validated private Record asset; never follow callback URLs."""
        require_sid(recording_sid, "RE")
        account = self.settings.account_sid
        key = self.settings.api_key or account
        secret = self.settings.api_secret or self.settings.auth_token
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account}/Recordings/{recording_sid}.wav"
        maximum = (self.settings.voicemail_max_seconds + 5) * 32000 + 65536
        async with httpx.AsyncClient(auth=(key, secret), timeout=15, follow_redirects=False) as http:
            async with http.stream("GET", url) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > maximum:
                        raise ValueError("Voicemail recording too large")
        if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise ValueError("Invalid voicemail audio")
        return bytes(data)

    async def read_status(self, call_sid: str) -> dict:
        """Read provider state without changing a call when callbacks were lost."""
        def fetch():
            call = self._client().calls(call_sid).fetch()
            raw = str(call.duration or "")
            duration = (int(raw) if raw.isascii() and raw.isdecimal() and len(raw) <= 6
                        else None)
            return {"status": call.status, "duration_seconds": duration}

        return await asyncio.to_thread(fetch)


class OperatorController:
    """Owns the Twilio legs, the per-session router, and the phase deadlines."""

    def __init__(self, settings, store: OperatorSessions, dialer=None, *, voice_id="",
                 voice=None, keypad_factory=Keypad, registry=None,
                 context_getter=None, on_call_start=None, on_call_end=None,
                 on_audio=None, on_output_audio=None, on_agent_turn=None,
                 provider_transport=None, voicemail_store=None):
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
        self.voicemails = voicemail_store
        self._provider_clients = {}
        self._ai_pending = set()
        self._auto_consumed = set()
        self._auto_suppressed = set()
        self._started = set()
        self._mark_waiters = {}
        self._transcript_seen = {}
        self._reply_timers = {}
        self._takeover_requests = {}
        self._accepted_takeovers = {}
        self._remote_revisions = {}
        self._remote_turn_pending = set()
        self._remote_turn_open = set()
        self._remote_turn_onsets = {}
        self._dialogue_runs = {}
        self._agent_reply_counts = {}
        self._voicemail_agents = {}
        self._voicemail_resume_reply = {}
        self._retired_owner_calls = set()
        self._caller_turn_floor = {}
        self._output_clocks = {}
        self._agent_frames_sent = {}
        self.announcement_cache = {}
        self._voicemail_greeting_tasks = {}
        self._voicemail_greeting_waiters = {}
        self._voicemail_warmers = {}
        self._disconnect_checks: dict[tuple[str, str], asyncio.Task] = {}
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

    @asynccontextmanager
    async def provider_client(self, session_id):
        """Reuse connections across replies; close them with the call session."""
        client = self._provider_clients.get(session_id)
        if client is None:
            client = httpx.AsyncClient(transport=self.provider_transport,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2,
                                   keepalive_expiry=120))
            self._provider_clients[session_id] = client
        yield client

    def on_detection(self, session_id, result):
        """Accept only validated live caller evidence from the in-process detector."""
        if (not self.settings.automatic_takeover_enabled or not isinstance(result, dict)
                or result.get('provider') != 'modulate'):
            return
        session = self.store.find(session_id)
        if session is None or not session.active or getattr(session, 'voicemail', False):
            return
        try:
            analysis = validate_analysis(result.get('analysis'))
        except (TypeError, ValueError, KeyError):
            return
        if (analysis['version'] != 3 or analysis['source'] != 'live'
                or analysis['track'] != 'inbound'
                or analysis['min_confidence'] < self.settings.modulate_detection_min_confidence):
            return
        if analysis['alert'] != 'ai_detected':
            # An invalidated provider stream can retract provisional evidence
            # while the owner is ringing. Do not activate from that stale flag.
            self._ai_pending.discard(session_id)
            return
        self._ai_pending.add(session_id)
        self._maybe_auto_takeover(session_id)

    def _maybe_auto_takeover(self, session_id):
        session = self.store.find(session_id)
        if (not self.settings.automatic_takeover_enabled or session_id not in self._ai_pending
                or session_id in self._auto_consumed or session_id in self._auto_suppressed
                or session is None or not session.active or session.phase != CONNECTED
                or session.mode != HUMAN or getattr(session, 'voicemail', False)
                or not self.voice_ready or not all(self.router(session_id).attached(r) for r in ROLES)):
            return
        # Consume before the first await: repeated streaming verdicts cannot
        # race a slow voice lookup or retry failed provider work indefinitely.
        self._auto_consumed.add(session_id)
        request_id = self._takeover_requests.get(session_id, 0) + 1
        self._takeover_requests[session_id] = request_id
        self.store.spawn(self._activate_internal(session_id, 'ai-detected', request_id))

    async def _internal_snapshot(self, kind):
        return await asyncio.to_thread(internal_snapshot, self.registry, kind)

    async def _activate_internal(self, session_id, kind, request_id):
        session = self.store.find(session_id)
        def current():
            return bool(session and session.active
                and self._takeover_requests.get(session_id) == request_id
                and (kind == 'voicemail' or session_id not in self._auto_suppressed))
        try:
            async with asyncio.timeout(3):
                snapshot = await self._internal_snapshot(kind)
                if self.context_getter is not None:
                    await maybe_await(self.context_getter(session))
            if not current() or (kind == 'ai-detected' and session_id not in self._ai_pending):
                return False
            router = self.router(session_id)
            roles = (REMOTE,) if kind == 'voicemail' else ROLES
            if session.phase != CONNECTED or not all(router.attached(role) for role in roles):
                raise OperatorRejected('call-not-connected')
            await self.store.select_profile(session_id, 'voiceml' if kind == 'voicemail' else 'aiwatch', mode=PREPARING)
            if not current():
                return False
            if kind == 'ai-detected' and session_id not in self._ai_pending:
                # select_profile may await the session lock. If evidence was
                # withdrawn meanwhile, undo only this still-authorized change;
                # a newer manual command must retain its own mode and profile.
                await self._release(session_id)
                return False
            self._stop_playback(session_id)
            timer = self._reply_timers.pop(session_id, None)
            if timer:
                timer.cancel()
            self._note_cleared(session_id, router.set_mode(PREPARING))
            session.agent_snapshot, session.agent_name, session.voice_id = snapshot, snapshot.name, snapshot.voice_id
            session.agent_kind = kind
            self._accepted_takeovers[session_id] = request_id
            self._agent_reply_counts[session_id] = 0
            self._caller_turn_floor.pop(session_id, None)
            if kind == 'voicemail':
                self._voicemail_agents[session_id].reply_started('greeting')
            self.trace(session_id, 'internal-takeover', kind=kind, agent_revision=snapshot.revision)
            self._start_dialogue(session, announce=True)
            return True
        except Exception as exc:
            self.trace(session_id, 'internal-takeover-failed', kind=kind, error=type(exc).__name__)
            if current():
                await self._abandon(session_id, 'internal-agent-unavailable')
            return False

    async def _retire_voicemail_owner(self, call_sid):
        if call_sid and call_sid not in self._retired_owner_calls:
            self._retired_owner_calls.add(call_sid)
            await self._safe_end_call(OWNER, call_sid)

    async def _begin_voicemail(self, session_id, reason):
        session = self.store.find(session_id)
        if session is None or not session.active:
            return False
        if session.voicemail:
            return True
        if not await self.store.claim_voicemail(session_id, reason):
            return False
        self.trace(session_id, 'voicemail-started', reason=reason)
        if self.voicemails is not None:
            self.voicemails.start(session.canonical_call_sid, reason, mode="voicemail_ai",
                                  started_at=session.created_at)
        self.router(session_id).stop_cue()
        vm = VoicemailAgent(session,
            on_reply=lambda phase: self._voicemail_reply(session_id, phase),
            on_end=lambda why: (self.fallback_voicemail(session_id, why)
                if why == "voicemail-reply-unavailable" else self.end(session_id, why)))
        self._voicemail_agents[session_id] = vm
        vm.start()
        if session.legs[OWNER].call_sid:
            self.store.spawn(self._retire_voicemail_owner(session.legs[OWNER].call_sid))
        if not self.voice_ready:
            await self.fallback_voicemail(session_id, "voice-unavailable")
        elif self.router(session_id).attached(REMOTE):
            await self._resume_voicemail(session_id)
        return True

    async def fallback_voicemail(self, session_id, reason):
        """Claim once before awaiting providers; late STT cannot restart the AI."""
        session = self.store.find(session_id)
        if session is None or not session.active or not session.voicemail:
            return False
        if session.voicemail_fallback:
            return True
        vm = self._voicemail_agents.get(session_id)
        close_with_message = bool(vm and vm.has_final_message and not vm.utterance_open)
        session.voicemail_fallback = True
        session.voicemail_phase = "complete" if close_with_message else "recording"
        self._cancel_voicemail_warmup(session_id)
        self._takeover_requests[session_id] = self._takeover_requests.get(session_id, 0) + 1
        self._accepted_takeovers.pop(session_id, None)
        vm = self._voicemail_agents.pop(session_id, None)
        if vm is not None:
            vm.close()
        self._voicemail_resume_reply.pop(session_id, None)
        await self.store.invalidate_reply(session_id)
        self._stop_playback(session_id)
        self._note_cleared(session_id, self.router(session_id).clear(*ROLES))
        self._note_interrupted(session_id)
        self.router(session_id).stop_cue()
        if self.voicemails is not None and not close_with_message:
            self.voicemails.fallback(session.canonical_call_sid, reason)
        self.trace(session_id, "voicemail-message-preserved" if close_with_message
                   else "voicemail-recording-fallback", reason=reason)
        try:
            async with asyncio.timeout(12):
                await self.dialer.replace_twiml(session.canonical_call_sid,
                    voicemail_closing_twiml() if close_with_message else
                    voicemail_recording_twiml(self.settings, session.canonical_call_sid))
            # Twilio must speak the native farewell before Hangup. Its signed
            # terminal callback (or existing status recovery) finishes storage.
            return True
        except Exception as exc:
            self.trace(session_id, "voicemail-fallback-failed", error=type(exc).__name__)
            if self.voicemails is not None:
                self.voicemails.fail_fallback(session.canonical_call_sid)
            await self.end(session_id, "voicemail-closing-unavailable" if close_with_message
                           else "voicemail-recording-unavailable")
            return False

    async def _resume_voicemail(self, session_id):
        session = self.store.find(session_id)
        vm = self._voicemail_agents.get(session_id)
        if session is None or not session.active or vm is None or session.voicemail_fallback:
            return
        self.router(session_id).stop_cue()
        if session.agent_snapshot is None:
            request = self._takeover_requests.get(session_id, 0) + 1
            self._takeover_requests[session_id] = request
            self.store.spawn(self._activate_internal(session_id, 'voicemail', request))
        elif self._voicemail_resume_reply.pop(session_id, False):
            await self.store.set_mode(session_id, PREPARING)
            self.router(session_id).set_mode(PREPARING)
            vm.reply_started(session.voicemail_phase or 'greeting')
            self._start_dialogue(session, announce=True)
        else:
            vm.resume_listening()

    async def _voicemail_reply(self, session_id, phase):
        session = self.store.find(session_id)
        if (session is None or not session.active or not session.voicemail
                or not self.router(session_id).attached(REMOTE)):
            return
        if self.playing(session_id):
            raise OperatorRejected('voicemail-reply-already-playing')
        self.trace(session_id, 'voicemail-reply-requested', phase=phase)
        session.voicemail_phase = phase
        await self.store.invalidate_reply(session_id)
        self._start_dialogue(session)

    async def voicemail_reply_completed(self, session_id, phase):
        vm = self._voicemail_agents.get(session_id)
        if vm is not None:
            await vm.reply_completed(phase)

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

    def trace(self, session_id, event, **fields):
        """Persist timing and control evidence without prompts, audio, or secrets."""
        session = self.store.find(session_id)
        if session is None:
            return
        log.info("operator_trace %s", json.dumps({"call_sid": session.canonical_call_sid,
            "session": session_id, "event": event, "elapsed_ms": self.elapsed_ms(session_id),
            "epoch": session.reply_epoch, "mode": session.mode, **fields}, separators=(",", ":")))

    def _same_takeover(self, session_id, slot):
        session = self.store.find(session_id)
        return bool(session and session.active and session.profile == slot
                    and session.agent_snapshot is not None
                    and self._accepted_takeovers.get(session_id) == self._takeover_requests.get(session_id)
                    and session.mode in (PREPARING, ANNOUNCING, AGENT))

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
            run = self._dialogue_runs.get(session_id)
            if (run is not None and run.current() and session.mode == AGENT
                    and run.pending is not None and run.speaking_started_ms is None
                    and getattr(frame, "reply_epoch", None) == run.epoch):
                run.speaking_started_ms = self.elapsed_ms(session_id)
                self._caller_turn_floor[session_id] = run.speaking_started_ms
                run.trace("reply-first-frame-sent")
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
        waiter = self._mark_waiters.get((session_id, role, name))
        if waiter is not None and not waiter.done():
            if recorded == "played":
                waiter.set_result(True)
            elif recorded != "pending":
                waiter.set_exception(OperatorRejected("playback-interrupted"))

    async def wait_for_mark(self, session_id, name, *, timeout=6.0, role=REMOTE):
        future = asyncio.get_running_loop().create_future()
        key = (session_id, role, name)
        self._mark_waiters[key] = future
        try:
            if not await self.router(session_id).mark(role, name):
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

    async def start_inbound(self, call_sid, caller, call_token=""):
        session, reused = await self.store.reserve_inbound(call_sid, caller, call_token)
        if not reused:
            self.store.spawn(self._dial_owner(session.id))
            if getattr(self.settings, 'voicemail_agent_enabled', False) and self.voice_ready:
                task = self.store.spawn(self._warm_voicemail_greeting(session.id))
                self._voicemail_warmers[session.id] = task
                def finished(done):
                    if self._voicemail_warmers.get(session.id) is done:
                        self._voicemail_warmers.pop(session.id, None)
                task.add_done_callback(finished)
        return session

    async def _warm_voicemail_greeting(self, session_id):
        """Prepare only fixed audio while ringing; never activate the agent."""
        try:
            async with asyncio.timeout(PREPARATION_SECONDS):
                snapshot = await self._internal_snapshot('voicemail')
                session = self.store.find(session_id)
                if session is None or not session.active or (session.phase == CONNECTED and not session.voicemail):
                    return
                await voicemail_greeting_audio(self, snapshot)
                self.trace(session_id, 'voicemail-greeting-ready')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Warm-up failure must not disrupt human ringing. The actual
            # voicemail attempt still gets its normal bounded failure path.
            self.trace(session_id, 'voicemail-greeting-warm-failed', error=type(exc).__name__)

    def _cancel_voicemail_warmup(self, session_id):
        task = self._voicemail_warmers.pop(session_id, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        return task

    def inbound_twiml(self, session):
        leg = session.legs[REMOTE]
        return leg_twiml(self.settings.public_base_url, session.id, REMOTE,
                         leg.generation, leg.token, prompt="New College Data Science")

    async def _dial_owner(self, session_id: str):
        if not await self.store.begin_owner_dial(session_id):
            return
        session = self.store.sessions.get(session_id)
        if session is None:
            return
        leg = session.legs[OWNER]
        twiml = leg_twiml(self.settings.public_base_url, session_id, OWNER,
                          leg.generation, leg.token,
                          prompt=ACCEPT_PROMPT if session.direction == "outbound" else None)
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
            forwarding = ({"caller_number": session.to, "call_token": session.call_token}
                          if session.direction == "inbound" and role == OWNER
                          and session.call_token else {})
            call_sid = await self.dialer.create_leg(
                to=destination, twiml=twiml,
                status_callback=self.settings.public_base_url
                + f"/twilio/status/{session_id}/{role}", **forwarding)
            await self.store.bind_call_sid(session_id, role, call_sid)
        except OperatorRejected as exc:
            log.warning("operator_dial_rejected session=%s role=%s reason=%s",
                        session_id, role, exc.reason)
            if session.voicemail and role == OWNER:
                if isinstance(call_sid, str) and CALL_SID.fullmatch(call_sid):
                    self.store.spawn(self._retire_voicemail_owner(call_sid))
                return
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
        self._cancel_disconnect_check(session_id, role)
        session = self.store.sessions.get(session_id)
        if session is None or not session.active:
            return
        if session.voicemail:
            if role == REMOTE:
                await self._notify_started(session)
                if not session.active:
                    return
                await self._resume_voicemail(session_id)
            return
        if session.direction == "inbound":
            if role == REMOTE:
                await self._notify_started(session)
                # Storage/provider startup can yield to a terminal callback.
                # Never recreate the router that its cleanup just removed.
                if not session.active:
                    return
            router = self.router(session_id)
            # Answering the incoming call is the owner's acceptance. Either
            # stream may arrive first, but neither microphone crosses until
            # both signed, token-bound streams are attached. This changes only
            # the connection phase; agent takeover still requires owner #1–#9.
            if all(router.attached(participant) for participant in ROLES):
                if await self.store.mark_connected(session_id):
                    self._cancel_voicemail_warmup(session_id)
                    router.stop_cue()
                    log.info("operator_connected session=%s", session_id)
                self._maybe_auto_takeover(session_id)
            elif role == REMOTE and session.phase != CONNECTED:
                router.start_cue(REMOTE)
            return
        if role == OWNER:
            await self.store.mark_owner_prompt(session_id)
            return
        if not session.canonical_call_sid:
            session.canonical_call_sid = session.legs[REMOTE].call_sid
        await self._notify_started(session)
        if not session.active:
            return
        if await self.store.mark_connected(session_id):
            self.router(session_id).stop_cue()
            log.info("operator_connected session=%s", session_id)
        self._maybe_auto_takeover(session_id)

    async def stream_stopped(self, session_id: str, role: str, reason: str):
        # A stream ending is not a hangup: Twilio may still be running the
        # recovery redirect, and the call ends from its signed status callback.
        log.info("operator_stream_stopped session=%s role=%s reason=%s",
                 session_id, role, reason)
        session = self.store.sessions.get(session_id)
        if session is not None and session.voicemail:
            if role == OWNER:
                return  # A retired/late owner socket cannot affect voicemail.
            if not session.voicemail_fallback:
                vm = self._voicemail_agents.get(session_id)
                if vm is not None:
                    self._voicemail_resume_reply[session_id] = vm.suspend()
                if session.agent_snapshot is None:
                    # Revoke an in-flight initial voice/context lookup. It must
                    # not interpret this recoverable transport gap as a failed call.
                    self._takeover_requests[session_id] = self._takeover_requests.get(session_id, 0) + 1
                if session.active:
                    await self.store.invalidate_reply(session_id)
                    self._stop_playback(session_id)
                    self._note_cleared(session_id, self.router(session_id).clear(*ROLES))
                    self._note_interrupted(session_id)
            # Native Record deliberately replaces the stream, but still needs
            # terminal-status recovery when Twilio's callbacks are missed.
        elif session is not None and session.mode != HUMAN:
            await self._release(session_id)
        if session is not None and session.active and hasattr(self.dialer, "read_status"):
            leg = session.legs[role]
            if not leg.attached and leg.call_sid:
                self._cancel_disconnect_check(session_id, role)
                self._disconnect_checks[(session_id, role)] = self.store.spawn(
                    self._recover_disconnected_leg(session_id, role, leg.generation, leg.call_sid))

    def _cancel_disconnect_check(self, session_id: str, role: str):
        task = self._disconnect_checks.pop((session_id, role), None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _still_disconnected(self, session_id, role, generation, call_sid):
        session = self.store.find(session_id)
        leg = session.legs.get(role) if session is not None else None
        return bool(session and session.active and leg and not leg.attached
                    and not leg.ended and leg.generation == generation and leg.call_sid == call_sid)

    async def _recover_disconnected_leg(self, session_id, role, generation, call_sid):
        """Recover missed terminal callbacks, never infer hangup from a lost socket.

        The recovery redirect gets a grace period. Every result is checked
        against the original stream generation so an old read cannot end a
        reconnected call. REST timeouts and live statuses leave the call alone.
        """
        key = (session_id, role)
        try:
            for delay in DISCONNECT_STATUS_DELAYS:
                await asyncio.sleep(delay)
                if not self._still_disconnected(session_id, role, generation, call_sid):
                    return
                try:
                    status = await asyncio.wait_for(self.dialer.read_status(call_sid),
                                                    timeout=STATUS_FETCH_SECONDS)
                except Exception as exc:
                    log.warning("operator_disconnect_status_failed session=%s role=%s type=%s",
                                session_id, role, type(exc).__name__)
                    continue
                if not self._still_disconnected(session_id, role, generation, call_sid):
                    return
                if status.get("status") not in TERMINAL_STATUSES:
                    continue
                result = await self.store.record_status(session_id, role, call_sid,
                    status["status"], duration=status.get("duration_seconds"))
                if result["action"] == "terminal":
                    log.info("operator_missed_status_recovered session=%s role=%s status=%s",
                             session_id, role, status["status"])
                    if role == OWNER and await self._begin_voicemail(session_id, result['reason']):
                        return
                    await self.end(session_id, f"{role}-{result['reason']}")
                return
        finally:
            if self._disconnect_checks.get(key) is asyncio.current_task():
                self._disconnect_checks.pop(key, None)

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
            self.trace(session_id, "shortcut", slot=command.value)
            if self._same_takeover(session_id, command.value):
                self.trace(session_id, "shortcut-already-active", slot=command.value)
                return
            # A published-slot lookup may wait on SQLite. Keep that work off
            # the phone's media reader, including while its result is awaited.
            request_id = self._takeover_requests.get(session_id, 0) + 1
            self._takeover_requests[session_id] = request_id
            self.store.spawn(self._take_over(session_id, command.value, request_id=request_id))
        elif command.kind == RELEASE:
            self.trace(session_id, "release-shortcut")
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
        self._auto_suppressed.add(session_id)
        expected = request_id if request_id is not None else self._takeover_requests.get(session_id, 0) + 1
        try:
            return await self._prepare_takeover(session_id, slot, request_id=request_id)
        except OperatorRejected:
            # A failed replacement must neither grant the old agent the new
            # request's authority nor leave a silently suspended agent in control.
            session = self.store.find(session_id)
            if (session is not None and session.active
                    and session.mode in (PREPARING, ANNOUNCING, AGENT)
                    and self._takeover_requests.get(session_id) == expected
                    and self._accepted_takeovers.get(session_id) != expected):
                await self._release(session_id)
            raise

    async def _prepare_takeover(self, session_id, slot, *, request_id=None):
        session = self.store.find(session_id)
        if session is None or not session.active:
            raise OperatorRejected("unknown-session")
        if not self.voice_ready:
            raise OperatorRejected("voice-not-ready")
        if not isinstance(slot, str) or slot not in tuple(str(i) for i in range(1, 10)):
            raise OperatorRejected("invalid-slot")
        if self._same_takeover(session_id, slot):
            return False
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
        session.agent_kind = 'manual'
        session.agent_name = snapshot.name
        session.voice_id = snapshot.voice_id
        self._accepted_takeovers[session_id] = request_id
        self._agent_reply_counts[session_id] = 0
        self._remote_turn_pending.discard(session_id)
        self._remote_turn_open.discard(session_id)
        self._remote_turn_onsets.pop(session_id, None)
        self._caller_turn_floor.pop(session_id, None)
        self.trace(session_id, "takeover-preparing", slot=slot,
                   agent_id=snapshot.id, agent_revision=snapshot.revision)
        self._start_dialogue(session, announce=True)
        return True

    def _start_dialogue(self, session, *, announce=False):
        run = DialogueRun(self, session, session.agent_snapshot, session.reply_epoch,
                          announce=announce)
        task = self.store.spawn(run.run())
        self.players[session.id] = task
        self._dialogue_runs[session.id] = run
        def done(finished):
            if self.players.get(session.id) is finished:
                self.players.pop(session.id, None)
                self._dialogue_runs.pop(session.id, None)
                # Keep the initial handoff moving; ordinary replies must still
                # consume a complete new caller turn received during preparation.
                # An unfinished utterance remains gated by _schedule_reply.
                pending_turn = (not run.announce and session.id in self._remote_turn_pending)
                if (run.completed and run.current() and (run.end_deferred or pending_turn)
                        and run.takeover_request == self._takeover_requests.get(session.id, 0)
                        and self._remote_revisions.get(session.id, 0) > run.remote_revision):
                    self._schedule_reply(session.id)
        task.add_done_callback(done)

    def _schedule_reply(self, session_id, *, delay=0.3):
        session = self.store.find(session_id)
        if session_id in self._remote_turn_open:
            return  # A deferred hangup still waits for a complete caller utterance.
        if session is not None and session.voicemail:
            # Voicemail has its own quiet-period policy. Never feed the last
            # transcript back as if the caller said it again.
            return
        timer = self._reply_timers.pop(session_id, None)
        if timer is not None:
            timer.cancel()
        self._reply_timers[session_id] = self.store.spawn(self._after_remote_turn(session_id, delay=delay))

    async def transcript(self, session_id, speaker, text, *, final=True,
                         segment_id="", timestamp_ms=None, speech_final=None,
                         speech_started=False, turn_end_ms=None):
        """Receive canonical STT updates; only remote speech drives dialogue."""
        session = self.store.find(session_id)
        text = str(text or "").strip()
        if (session is None or not session.active or session.voicemail_fallback
                or (not text and not speech_started and speech_final is not True)
                or speaker not in (OWNER, REMOTE)):
            return
        if final and text and segment_id:
            seen = self._transcript_seen.setdefault(session_id, {})
            if segment_id in seen:
                return
            seen[segment_id] = True
            while len(seen) > 1000:
                seen.pop(next(iter(seen)))
        if final and text:
            if speaker == REMOTE:
                self._remote_revisions[session_id] = self._remote_revisions.get(session_id, 0) + 1
            await self.store.add_turn(session_id, speaker, text)
            # Preserve new context while keeping the manual handoff moving.
        if speaker == REMOTE and session.mode == AGENT:
            floor = self._caller_turn_floor.get(session_id)
            observed_onset = self._remote_turn_onsets.get(session_id)
            continuing_turn = (session_id in self._remote_turn_open
                and timestamp_ms is not None and observed_onset is not None
                and timestamp_ms >= observed_onset)
            if (timestamp_ms is not None and floor is not None and timestamp_ms < floor
                    and not continuing_turn):
                return
            if session.voicemail and (speech_final is True or (final and text)):
                self.trace(session_id, 'voicemail-caller-endpoint' if speech_final is True
                           else 'voicemail-caller-final', speech_start_ms=timestamp_ms,
                           speech_end_ms=turn_end_ms, has_text=bool(text))
            activity = bool(text or speech_started)
            if speech_started:
                self.trace(session_id, "caller-speech-started", speech_start_ms=timestamp_ms)
            if speech_final is True or (speech_final is None and final and text):
                self._remote_turn_open.discard(session_id)
                self._remote_turn_onsets.pop(session_id, None)
            elif speech_final is False and activity:
                # A result keeps its original speech onset even if its final
                # arrives after agent playback advances the old-speech floor.
                # Remember only an onset already accepted by that floor, so a
                # genuinely older final cannot reopen or complete a new turn.
                if timestamp_ms is not None and (speech_started or observed_onset is None):
                    self._remote_turn_onsets[session_id] = timestamp_ms
                self._remote_turn_open.add(session_id)
            if final and text:
                self._remote_turn_pending.add(session_id)
            if activity:
                timer = self._reply_timers.pop(session_id, None)
                if timer is not None:
                    timer.cancel()
                if not final and text:
                    # Recognized words revoke a stale end-call action before
                    # finalization. Raw VAD can be noise or playback echo and
                    # must not by itself revoke a goodbye or clear agent audio.
                    self._remote_revisions[session_id] = self._remote_revisions.get(session_id, 0) + 1
            if text and self.playing(session_id):
                run = self._dialogue_runs.get(session_id)
                # Barge-in requires actual outgoing speech, not a running HTTP
                # request. Delayed STT from before playback is not a new interruption.
                if (run is not None and (run.speaking_started_ms is None
                        or (timestamp_ms is not None and timestamp_ms < run.speaking_started_ms))):
                    vm = self._voicemail_agents.get(session_id)
                    if vm is not None:
                        vm.transcript(text, final=final, speech_started=speech_started,
                                      speech_final=speech_final)
                    return
                self.trace(session_id, "caller-interruption",
                           source="final-transcript" if final else "interim-transcript")
                await self.store.invalidate_reply(session_id)
                self._stop_playback(session_id)
                self._note_cleared(session_id, self.router(session_id).clear(*ROLES))
                self._note_interrupted(session_id)
                vm = self._voicemail_agents.get(session_id)
                if vm is not None:
                    vm.interrupted()
            vm = self._voicemail_agents.get(session_id)
            if vm is not None:
                vm.transcript(text, final=final, speech_started=speech_started,
                              speech_final=speech_final)
                return
            # Production STT separates finalized chunks from an actual endpoint.
            # None retains the contract for direct/legacy callers without this
            # metadata; Deepgram always supplies an explicit boolean.
            ended = speech_final is True or (speech_final is None and final and bool(text))
            if (ended and session_id in self._remote_turn_pending
                    and session.agent_snapshot is not None):
                if speech_final is True:
                    self.trace(session_id, "caller-turn-ended", boundary_ms=turn_end_ms)
                self._schedule_reply(session_id, delay=0.1 if speech_final is True else 0.3)
        elif speaker == REMOTE and session.voicemail:
            vm = self._voicemail_agents.get(session_id)
            if vm is not None:
                vm.transcript(text, final=final, speech_started=speech_started,
                              speech_final=speech_final)

    async def _after_remote_turn(self, session_id, *, delay=0.3):
        try:
            await asyncio.sleep(delay)
            session = self.store.find(session_id)
            if (session is not None and session.active and session.mode == AGENT
                    and not self.playing(session_id)
                    and self._accepted_takeovers.get(session_id) == self._takeover_requests.get(session_id)):
                self._remote_turn_pending.discard(session_id)
                self._remote_turn_open.discard(session_id)
                self._remote_turn_onsets.pop(session_id, None)
                await self.store.invalidate_reply(session_id)
                self._start_dialogue(session)
        finally:
            if self._reply_timers.get(session_id) is asyncio.current_task():
                self._reply_timers.pop(session_id, None)

    async def _release(self, session_id: str):
        """``#0`` returns control immediately and never waits on a provider."""
        session = self.store.find(session_id)
        if session is not None and session.voicemail:
            raise OperatorRejected('voicemail-has-no-connected-owner')
        self.trace(session_id, "return-to-human")
        self._auto_suppressed.add(session_id)
        self._remote_turn_pending.discard(session_id)
        self._remote_turn_open.discard(session_id)
        self._remote_turn_onsets.pop(session_id, None)
        self._takeover_requests[session_id] = self._takeover_requests.get(session_id, 0) + 1
        self._accepted_takeovers.pop(session_id, None)
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
        self.trace(session_id, "takeover-failed", reason=reason)
        log.warning("operator_takeover_abandoned session=%s reason=%s", session_id, reason)
        session = self.store.find(session_id)
        if session is not None and session.voicemail:
            await self.fallback_voicemail(session_id, reason)
            return
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
        """Whether generation or playback is still in progress for this session."""
        task = self.players.get(session_id)
        return bool(task is not None and not task.done())

    def _stop_playback(self, session_id: str):
        """Cancel the phrase being spoken; the epoch change already invalidates it."""
        task = self.players.pop(session_id, None)
        self._dialogue_runs.pop(session_id, None)
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
        vm = self._voicemail_agents.pop(session_id, None)
        if vm is not None:
            vm.close()
        self._voicemail_resume_reply.pop(session_id, None)
        self._cancel_voicemail_warmup(session_id)
        session = self.store.find(session_id)
        if session is not None:
            self._retired_owner_calls.discard(session.legs[OWNER].call_sid)
        for role in ROLES:
            self._cancel_disconnect_check(session_id, role)
        for table in (self.keys, self._reply_timers):
            task = table.pop(session_id, None)
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
        self._stop_playback(session_id)
        self.keypads.pop(session_id, None)
        self._transcript_seen.pop(session_id, None)
        self._takeover_requests.pop(session_id, None)
        self._accepted_takeovers.pop(session_id, None)
        self._remote_revisions.pop(session_id, None)
        self._remote_turn_pending.discard(session_id)
        self._remote_turn_open.discard(session_id)
        self._remote_turn_onsets.pop(session_id, None)
        self._agent_reply_counts.pop(session_id, None)
        self._caller_turn_floor.pop(session_id, None)
        self._ai_pending.discard(session_id)
        self._auto_consumed.discard(session_id)
        self._auto_suppressed.discard(session_id)
        self._output_clocks.pop(session_id, None)

    async def _on_session_end(self, session_id: str):
        player = self.players.get(session_id)
        warmer = self._voicemail_warmers.get(session_id)
        self._stop_session_tasks(session_id)
        router = self.routers.pop(session_id, None)
        if router is not None:
            router.close()
        if player is not None and player is not asyncio.current_task():
            await asyncio.gather(player, return_exceptions=True)
        if warmer is not None and warmer is not asyncio.current_task():
            await asyncio.gather(warmer, return_exceptions=True)
        client = self._provider_clients.pop(session_id, None)
        if client is not None:
            await client.aclose()
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
        if reason == 'owner-no-answer' and await self._begin_voicemail(session.id, reason):
            return
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
                             provider_transport=None, voicemail_store=None) -> OperatorController:
    """Mount the operator bridge beside the existing conference path."""
    controller = OperatorController(settings, store, dialer, voice=voice, registry=registry,
        context_getter=context_getter, on_call_start=on_call_start, on_call_end=on_call_end,
        on_audio=on_audio, on_output_audio=on_output_audio, on_agent_turn=on_agent_turn,
        provider_transport=provider_transport, voicemail_store=voicemail_store)
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

    @app.get("/api/calls/config")
    async def call_config(request: Request):
        # Public demo access to agent settings never confers calling authority.
        # A read needs no Origin header; the existing write routes still require
        # the real owner cookie plus the same-origin request marker.
        managed = bool(getattr(settings, "agent_management_enabled", False)
                       and registry is not None and registry.enabled)
        authenticated = managed and await asyncio.to_thread(owner_authenticated, request, registry)
        result = {"authenticated": bool(authenticated),
                  "enabled": bool(managed and store.ready and (store.allowed or store.allowed_countries)),
                  "owner_label": None, "destinations": [], "countries": [],
                  "active_session": None, "busy": False}
        if authenticated:
            active = next((session for session in store.sessions.values()
                           if session.active and session.direction == "outbound"), None)
            result.update(owner_label=("•••• " + settings.owner_number[-4:]
                                       if settings.owner_number else None),
                          destinations=list(settings.allowed_destinations),
                          countries=sorted(store.allowed_countries),
                          active_session=active.to_status() if active is not None else None,
                          busy=store.active_count > 0)
        return JSONResponse(result, headers=SAFE_HEADERS)

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
            raise HTTPException(400, 'Expected {"to": "<permitted E.164>", "goal": "..."}')
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
            if role == OWNER and await controller._begin_voicemail(session_id, result['reason']):
                return Response(status_code=204)
            await controller.end(session_id, f"{role}-{result['reason']}")
            log.info("operator_call_ended session=%s role=%s reason=%s",
                     session_id, role, result["reason"])
        elif result['action'] == 'retired' and result.get('status') not in TERMINAL_STATUSES:
            store.spawn(controller._retire_voicemail_owner(call_sid))
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
        if session.voicemail_fallback and role == REMOTE:
            if session.voicemail_phase == "complete":
                return _xml(voicemail_closing_twiml())
            return _xml(voicemail_recording_twiml(settings, call_sid))
        if session.voicemail and role == OWNER:
            response = VoiceResponse()
            response.hangup()
            return _xml(response)
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

    async def fallback_receipt(call_sid, form):
        require_sid(call_sid)
        if require_sid(form.get("CallSid")) != call_sid or voicemail_store is None:
            raise HTTPException(400, "Voicemail callback does not match the call")
        await voicemail_store.restore(call_sid)
        receipt = await asyncio.to_thread(voicemail_store.get, call_sid)
        if receipt is None or receipt["mode"] != "voicemail_fallback":
            raise HTTPException(400, "Unknown voicemail recording")
        return receipt

    async def retire_fallback(call_sid, reason):
        session = next((s for s in store.sessions.values()
                        if s.canonical_call_sid == call_sid and s.voicemail_fallback and s.active), None)
        if session is not None:
            await controller.end(session.id, reason)

    @app.post("/twilio/voicemail/finished/{call_sid}")
    async def fallback_finished(call_sid: str, form=Depends(validate_twilio)):
        await fallback_receipt(call_sid, form)
        voicemail_store.finish(call_sid)
        await retire_fallback(call_sid, "voicemail-recording-finished")
        response = VoiceResponse()
        response.hangup()
        return _xml(response)

    @app.post("/twilio/voicemail/recording/{call_sid}")
    async def fallback_recording(call_sid: str, form=Depends(validate_twilio)):
        await fallback_receipt(call_sid, form)
        recording_sid = require_sid(form.get("RecordingSid"), "RE")
        if form.get("RecordingSource", "RecordVerb") != "RecordVerb":
            raise HTTPException(400, "Unexpected recording source")
        raw = str(form.get("RecordingDuration", ""))
        if raw and (not raw.isascii() or not raw.isdecimal() or len(raw) > 4):
            raise HTTPException(400, "Invalid recording duration")
        status = str(form.get("RecordingStatus", ""))
        if not voicemail_store.recording(call_sid, recording_sid, status, int(raw) if raw else None):
            raise HTTPException(400, "Unknown or mismatched voicemail recording")
        if status in {"completed", "absent", "failed"}:
            # Caller hangup can skip Record's action callback. The signed
            # terminal recording callback must also release bridge capacity.
            await retire_fallback(call_sid, f"voicemail-recording-{status}")
        return Response(status_code=204)

    @app.get("/api/voicemails/{call_sid}/audio")
    async def fallback_audio(call_sid: str):
        require_sid(call_sid)
        receipt = (await asyncio.to_thread(voicemail_store.get, call_sid)
                   if voicemail_store is not None else None)
        if (receipt is None or receipt["mode"] != "voicemail_fallback"
                or receipt["recording_status"] != "completed" or not receipt["recording_sid"]):
            raise HTTPException(404, "Voicemail audio is not available")
        try:
            async with asyncio.timeout(20):
                audio = await controller.dialer.recording_audio(receipt["recording_sid"])
        except Exception:
            raise HTTPException(502, "Voicemail audio is temporarily unavailable") from None
        return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{call_sid}-voicemail.wav"'})

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
