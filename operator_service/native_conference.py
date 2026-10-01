"""Opt-in phone conference pilot: human audio never passes through Python.

Passive human sockets are observers. The only writable socket belongs to a
separate TwiML App participant. Phone commands use Dial's native star escape
and Gather, rather than pretending passive Media Streams carry DTMF.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hmac
import json
import logging
import secrets
import time

from fastapi import Depends, HTTPException, Request, WebSocket
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse
from twilio.base.exceptions import TwilioRestException

from webhooks import require_sid, valid_media_signature
from voice_stack.audio import FRAME_MS, iter_frames
from .codecs import decode_payload
from .audio import CallRouter, MAX_BAD_MESSAGES, MAX_MESSAGE_BYTES
from .conference_gateway import (native_agent_twiml, native_leg_twiml,
                                 owner_accept_twiml, owner_menu_twiml)
from .sessions import (AGENT, ANNOUNCING, HUMAN, OWNER, REMOTE, CONNECTED,
                       CALL_SID, STREAM_SID, TOKEN_SECONDS, valid_media_format,
                       OperatorRejected)

log = logging.getLogger("uvicorn.error")


@dataclass
class ConferenceState:
    sid: str = ""
    participants: set = field(default_factory=set)
    owner_menu: bool = False
    owner_reconcile_pending: bool = False
    observer_restarts: set = field(default_factory=set)
    bot_generation: int = 1
    bot_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    bot_issued: float = field(default_factory=time.monotonic)
    bot_participant: str = ""
    bot_stream_call: str = ""
    bot_application_call: str = ""
    bot_gone: bool = False
    bot_stream: str = ""
    bot_token_used: bool = False
    bot_ready: asyncio.Event = field(default_factory=asyncio.Event)
    bot_task: asyncio.Task | None = None
    control: asyncio.Lock = field(default_factory=asyncio.Lock)
    events: set = field(default_factory=set)
    sequences: dict = field(default_factory=dict)


class NativeConferenceRouter(CallRouter):
    """Keep the dialogue writer API; never attach a human socket to a writer."""

    def attached(self, role):
        session = self.controller.store.find(self.session_id)
        state = self.controller.native_state(self.session_id)
        return bool(session and session.active and session.legs[role].attached
                    and (role in state.participants or (role == OWNER and state.owner_menu)))

    def forward(self, source, frame, timestamp_ms=None):
        raise RuntimeError("Native human audio must not enter the relay")

    def send_agent(self, frames, *, reply_epoch=None):
        # Twilio mixes this participant into both human legs, including the
        # muted owner's listening leg. No second copy is sent to the owner.
        sent = 0
        for frame in frames:
            if not isinstance(frame, bytes) or len(frame) != 160:
                raise ValueError("Agent audio must be complete μ-law frames")
            sent += bool(self.channels[REMOTE].send(frame, kind="agent", reply_epoch=reply_epoch))
        return sent

    def send_announcement(self, frame, *, role=REMOTE):
        return role == REMOTE and self.channels[REMOTE].send(frame, kind="announcement")

    def start_cue(self, *args, **kwargs):
        pass  # The native conference supplies waiting audio.

    async def serve_observer(self, websocket, role, store):
        await websocket.accept()
        leg, bad, started = None, 0, time.monotonic()
        try:
            while not self.closed:
                message = await self._receive(websocket, leg, started)
                if message is None or message["type"] == "websocket.disconnect":
                    break
                event = self._event(message)
                if event is None:
                    bad += 1
                elif event.get("event") == "connected":
                    continue
                elif event.get("event") == "start" and leg is None:
                    try:
                        leg = await store.bind_stream(self.session_id, role, event.get("start") or {})
                    except OperatorRejected:
                        break
                    # An observation socket has no OutputChannel attachment.
                    await self.controller.stream_started(self.session_id, role, leg.stream_sid)
                elif leg is not None and event.get("streamSid") != leg.stream_sid:
                    bad += 1
                elif leg is not None and event.get("event") == "media":
                    bad += self._observe(role, leg, event)
                elif leg is not None and event.get("event") == "stop":
                    break
                else:
                    bad += 1
                if bad >= MAX_BAD_MESSAGES:
                    break
        except (RuntimeError, OSError):
            pass
        finally:
            if leg is not None:
                await store.detach_socket(self.session_id, role, leg.generation)
                await self.controller.stream_stopped(self.session_id, role, "observer-disconnected")
            try:
                await websocket.close(code=1000 if leg else 1008)
            except (RuntimeError, OSError):
                pass

    @staticmethod
    def _event(message):
        raw = message.get("text")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
            return None
        try:
            event = json.loads(raw)
            return event if isinstance(event, dict) else None
        except (ValueError, TypeError):
            return None

    def _observe(self, role, leg, event):
        media = event.get("media")
        if not isinstance(media, dict):
            return 1
        track = media.get("track")
        if track not in ({"inbound", "outbound"} if role == REMOTE else {"inbound"}):
            return 1
        try:
            frames = tuple(iter_frames(decode_payload(event["media"].get("payload"))))
            raw_ms = int(event["media"]["timestamp"])
            if raw_ms < 0:
                return 1
        except (ValueError, TypeError, KeyError):
            return 1
        # Each stream generation has one offset, but inbound and outbound have
        # independent cursors: simultaneous tracks must not double the clock.
        key, cursor = (role, leg.generation), (role, track)
        if key not in self._source_offsets:
            previous = max((v for (r, _), v in self._source_last.items() if r == role), default=-FRAME_MS)
            self._source_offsets[key] = max(self.controller.elapsed_ms(self.session_id), previous + FRAME_MS) - raw_ms
        for index, frame in enumerate(frames):
            stamp = max(raw_ms + self._source_offsets[key] + index * FRAME_MS,
                        self._source_last.get(cursor, -FRAME_MS) + FRAME_MS)
            self._source_last[cursor] = stamp
            leg.counters["frames_in"] += 1
            self.controller.native_audio(self.session_id, role, track, frame, stamp)
        return 0

    async def serve_bot(self, websocket):
        await websocket.accept()
        state = self.controller.native_state(self.session_id)
        bound, bad, started = False, 0, time.monotonic()
        try:
            while not self.closed:
                message = await self._receive(websocket, state if bound else None, started)
                if message is None or message["type"] == "websocket.disconnect":
                    break
                event = self._event(message)
                if event is None:
                    bad += 1
                elif event.get("event") == "connected":
                    continue
                elif event.get("event") == "start" and not bound:
                    start = event.get("start") or {}
                    if not isinstance(start, dict):
                        break
                    params = start.get("customParameters") or {}
                    if not isinstance(params, dict):
                        break
                    if not self.controller.valid_bot_start(state, start, params):
                        break
                    state.bot_token_used = True
                    state.bot_stream_call, state.bot_stream = start["callSid"], start["streamSid"]
                    self.channels[REMOTE].attach(websocket, state.bot_stream, state.bot_generation, {})
                    bound = True
                    state.bot_ready.set()
                elif bound and event.get("streamSid") != state.bot_stream:
                    bad += 1
                elif bound and event.get("event") == "mark":
                    await self._mark_played(REMOTE, event)
                elif bound and event.get("event") == "media":
                    # This is a conference mix, not the isolated caller. It
                    # must never feed STT or AI detection a second time.
                    pass
                elif bound and event.get("event") == "stop":
                    break
                else:
                    bad += 1
                if bad >= MAX_BAD_MESSAGES:
                    break
        except (RuntimeError, OSError):
            pass
        finally:
            if bound:
                state.bot_gone = True
                state.bot_ready.clear()
                self.channels[REMOTE].detach()
                session = self.controller.store.find(self.session_id)
                if session and session.active and session.mode != HUMAN:
                    await self.controller._release(self.session_id)
            try:
                await websocket.close(code=1000 if bound else 1008)
            except (RuntimeError, OSError):
                pass


class NativeConferenceMixin:
    """Override only opted-in outbound phone sessions; preserve other paths."""

    def native_state(self, session_id):
        if not hasattr(self, "_native_states"):
            self._native_states = {}
        for old in list(self._native_states):
            if old not in self.store.sessions:
                self._native_states.pop(old, None)
        return self._native_states.setdefault(session_id, ConferenceState())

    def is_native(self, session_id):
        session = self.store.find(session_id)
        return bool(session and session.native_conference)

    def router(self, session_id):
        if not self.is_native(session_id):
            return super().router(session_id)
        if session_id not in self.routers:
            self.routers[session_id] = NativeConferenceRouter(session_id, self)
        return self.routers[session_id]

    async def start_outbound(self, to, goal, idempotency_key, *, browser_allowed=True):
        session, reused = await self.store.reserve_outbound(to, goal, idempotency_key,
                                                          voice_id=self.voice_id, browser_allowed=browser_allowed)
        if not reused:
            session.native_conference = bool(getattr(self.settings, "native_conference_enabled", False)
                                             and not session.browser_audio)
            if not session.browser_audio:
                self.store.spawn(self._dial_owner(session.id))
        return session, reused

    async def _dial_owner(self, session_id):
        if not self.is_native(session_id):
            return await super()._dial_owner(session_id)
        if await self.store.begin_owner_dial(session_id):
            session = self.store.find(session_id)
            await self._dial(session_id, OWNER, session.legs[OWNER].destination,
                             owner_accept_twiml(self.settings, session))

    async def _dial_remote(self, session_id):
        if not self.is_native(session_id):
            return await super()._dial_remote(session_id)
        session = self.store.find(session_id)
        await self._dial(session_id, REMOTE, session.to, native_leg_twiml(self.settings, session, REMOTE))

    async def stream_started(self, session_id, role, stream_sid):
        if not self.is_native(session_id):
            return await super().stream_started(session_id, role, stream_sid)
        session = self.store.find(session_id)
        if role == REMOTE:
            session.canonical_call_sid = session.legs[REMOTE].call_sid
            await self._notify_started(session)
        await self.native_connected(session_id)

    async def stream_stopped(self, session_id, role, reason):
        if not self.is_native(session_id):
            return await super().stream_stopped(session_id, role, reason)
        session = self.store.find(session_id)
        # Losing an observation socket never ends the native conversation.
        if session and session.active and session.mode != HUMAN:
            await self._release(session_id)
        state = self.native_state(session_id)
        if (session and session.active and role not in state.observer_restarts
                and hasattr(self.dialer, "restart_observer")):
            # One attempt per leg; a lost REST response can conceal success, so
            # never redial/retry it blindly. Human TwiML is never replaced.
            state.observer_restarts.add(role)
            leg = session.legs[role]
            self.store.spawn(self._restart_native_observer(session_id, role, leg.generation))
        if session and session.active and hasattr(self.dialer, "read_status"):
            leg = session.legs[role]
            self._cancel_disconnect_check(session_id, role)
            self._disconnect_checks[(session_id, role)] = self.store.spawn(
                self._recover_disconnected_leg(session_id, role, leg.generation, leg.call_sid))

    async def _restart_native_observer(self, session_id, role, generation):
        await asyncio.sleep(0.5)
        session = self.store.find(session_id)
        if not session or not session.active:
            return
        leg = session.legs[role]
        if leg.attached or leg.ended or leg.generation != generation:
            return
        try:
            leg = await self.store.rotate_token(session_id, role)
            self._cancel_disconnect_check(session_id, role)
            await self.dialer.restart_observer(leg.call_sid, session_id, role, leg.generation, leg.token)
            self.trace(session_id, "native-observer-restarted", role=role, generation=leg.generation)
        except Exception as exc:
            self.trace(session_id, "native-observer-restart-failed", role=role, error=type(exc).__name__)
        # A successful/uncertain create may never establish a socket. Continue
        # checking call status for missed terminal callbacks on this generation.
        if session.active and not leg.attached and hasattr(self.dialer, "read_status"):
            self._disconnect_checks[(session_id, role)] = self.store.spawn(
                self._recover_disconnected_leg(session_id, role, leg.generation, leg.call_sid))

    async def native_connected(self, session_id):
        session = self.store.find(session_id)
        state = self.native_state(session_id)
        if session and session.active and {OWNER, REMOTE} <= state.participants:
            await self.store.mark_connected(session_id)
            self._maybe_auto_takeover(session_id)

    def native_audio(self, session_id, role, track, frame, stamp):
        session = self.store.find(session_id)
        if self.native_audio_callback and session and session.active:
            try:
                self.native_audio_callback(session, role, track, frame, stamp)
            except Exception as exc:
                log.warning("native_observer_failed type=%s", type(exc).__name__)

    def valid_bot_start(self, state, start, params):
        if not isinstance(start, dict) or not isinstance(params, dict):
            return False
        token = params.get("token")
        return bool(isinstance(token, str) and hmac.compare_digest(token, state.bot_token)
            and not state.bot_token_used and not state.bot_gone and time.monotonic() - state.bot_issued <= TOKEN_SECONDS
            and str(params.get("generation")) == str(state.bot_generation)
            and start.get("accountSid") == self.settings.account_sid
            and CALL_SID.fullmatch(str(start.get("callSid", "")))
            and start.get("callSid") == state.bot_application_call
            and STREAM_SID.fullmatch(str(start.get("streamSid", "")))
            and valid_media_format(start.get("mediaFormat")))

    async def _provision_native_bot(self, session):
        state = self.native_state(session.id)
        state.bot_issued = time.monotonic()
        sid = await self.dialer.create_agent_participant(state.sid, session.id,
                                                         state.bot_generation, state.bot_token)
        state.bot_participant = sid
        if not session.active:
            await self._safe_end_call("agent", sid)

    async def _ensure_native_bot(self, session):
        state = self.native_state(session.id)
        if not state.sid or state.bot_gone:
            raise OperatorRejected("conference-not-ready")
        if state.bot_task is None:
            state.bot_task = self.store.spawn(self._provision_native_bot(session))
        try:
            async with asyncio.timeout(12):
                await asyncio.shield(state.bot_task)
                await state.bot_ready.wait()
        except Exception:
            raise OperatorRejected("agent-participant-unavailable") from None

    async def native_mutation(self, operation):
        """A canceled to_thread request may still mutate Twilio: finish it first.

        Keep the control lock until the bounded SDK request settles so a late
        owner-mute response cannot overtake a subsequent return-to-human.
        """
        task = self.store.spawn(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            finally:
                raise

    async def transition_audio_mode(self, session_id, epoch, mode):
        if not self.is_native(session_id):
            return await super().transition_audio_mode(session_id, epoch, mode)
        session, state = self.store.find(session_id), self.native_state(session_id)
        async with state.control:
            await self._ensure_native_bot(session)
            if not session.active or session.reply_epoch != epoch or session.mode == HUMAN:
                raise OperatorRejected("stale-reply")
            if not session.native_owner_muted:
                # Record restore intent before a request whose response can be
                # lost or whose awaiting task can be canceled.
                session.native_owner_muted = True
                if not state.owner_menu:
                    await self.native_mutation(self.dialer.mute_participant(state.sid, session.legs[OWNER].call_sid, True))
            await self.native_mutation(self.dialer.mute_participant(state.sid, state.bot_participant, False))
            if not await self.store.transition(session_id, epoch, mode):
                raise OperatorRejected("stale-reply")
            self.router(session_id).set_mode(mode)

    async def _release(self, session_id):
        if not self.is_native(session_id):
            return await super()._release(session_id)
        # Cancel generation and clear streamed speech before waiting for REST.
        await super()._release(session_id)
        session, state = self.store.find(session_id), self.native_state(session_id)
        if not session or not session.active:
            return
        try:
            async with state.control:
                if state.bot_participant and not state.bot_gone:
                    try:
                        await self.native_mutation(self.dialer.mute_participant(state.sid, state.bot_participant, True))
                    except TwilioRestException as exc:
                        if exc.status == 404:
                            state.bot_gone = True
                        else:
                            self.trace(session_id, "native-bot-mute-failed", error=type(exc).__name__)
                            await self._safe_end_call("agent", state.bot_participant)
                    except Exception as exc:
                        self.trace(session_id, "native-bot-mute-failed", error=type(exc).__name__)
                        await self._safe_end_call("agent", state.bot_participant)
                if session.native_owner_muted and not state.owner_menu:
                    await self.native_mutation(self.dialer.mute_participant(state.sid, session.legs[OWNER].call_sid, False))
                session.native_owner_muted = state.owner_menu
        except Exception as exc:
            self.trace(session_id, "native-resume-failed", error=type(exc).__name__)
            raise OperatorRejected("native-resume-failed") from None

    async def _on_session_end(self, session_id):
        session = self.store.find(session_id)
        if session and session.native_conference:
            state = self.native_state(session_id)
            # Retain the tombstone until the store prunes it: delayed signed
            # callbacks must not create a fresh conference identity.
            if state.bot_participant:
                await self._safe_end_call("agent", state.bot_participant)
            if state.sid:
                try:
                    await self.dialer.end_conference(state.sid)
                except Exception as exc:
                    log.warning("native_conference_end_failed type=%s", type(exc).__name__)
        await super()._on_session_end(session_id)


def register_native_routes(app, settings, store, controller, validate_twilio):
    def xml(value):
        return Response(str(value), media_type="application/xml", headers={"Cache-Control": "no-store"})

    def hangup():
        response = VoiceResponse()
        response.hangup()
        return xml(response)

    def session_for(session_id, form, role=None):
        session = store.find(session_id)
        if not session or not session.native_conference:
            raise HTTPException(400, "Unknown native session")
        if role:
            sid = require_sid(form.get("CallSid"))
            leg = session.legs[role]
            if leg.call_sid and leg.call_sid != sid:
                raise HTTPException(400, "Native callback does not match the leg")
        return session

    @app.post("/twilio/native-owner/{session_id}")
    async def accept_owner(session_id: str, form=Depends(validate_twilio)):
        session = session_for(session_id, form, OWNER)
        if not session.active or form.get("Digits") != "1":
            return hangup()
        await store.bind_call_sid(session_id, OWNER, form["CallSid"])
        await store.mark_owner_prompt(session_id)
        if await store.begin_remote_dial(session_id):
            store.spawn(controller._dial_remote(session_id))
        return xml(native_leg_twiml(settings, session, OWNER))

    @app.post("/twilio/native-menu/{session_id}")
    async def owner_menu(session_id: str, form=Depends(validate_twilio)):
        session = session_for(session_id, form, OWNER)
        if not session.active:
            return hangup()
        # Conference completion also invokes Dial's action; it is not a menu.
        if form.get("DialCallStatus") != "answered":
            await controller.end(session_id, "native-conference-finished")
            return hangup()
        controller.native_state(session_id).owner_menu = True
        session.native_owner_muted = True
        return xml(owner_menu_twiml(settings, session))

    @app.post("/twilio/native-command/{session_id}")
    async def owner_command(session_id: str, form=Depends(validate_twilio)):
        session = session_for(session_id, form, OWNER)
        if not session.active:
            return hangup()
        state = controller.native_state(session_id)
        if not state.owner_menu:
            raise HTTPException(409, "The phone menu is not open")
        digit = str(form.get("Digits", ""))
        if digit == "0":
            await controller._release(session_id)
        elif digit in tuple(str(i) for i in range(1, 10)) and session.phase == CONNECTED:
            request_id = controller._takeover_requests.get(session_id, 0) + 1
            controller._takeover_requests[session_id] = request_id
            store.spawn(controller._take_over(session_id, digit, request_id=request_id))
        # Leave owner_menu true until the signed rejoin callback. A mute
        # operation while the owner is outside the conference would get 404.
        return xml(native_leg_twiml(settings, session, OWNER, rejoin=True))

    @app.post("/twilio/native-conference/{session_id}")
    async def conference_event(session_id: str, form=Depends(validate_twilio)):
        session = session_for(session_id, form)
        state = controller.native_state(session_id)
        sid = require_sid(form.get("ConferenceSid"), "CF")
        if form.get("FriendlyName") != "phoney-" + session_id:
            raise HTTPException(400, "Conference name does not match")
        if state.sid and state.sid != sid:
            raise HTTPException(400, "Conference callback does not match")
        call_sid = form.get("CallSid", "")
        event = form.get("StatusCallbackEvent", "")
        role = next((r for r in (OWNER, REMOTE) if session.legs[r].call_sid == call_sid), None)
        label = form.get("ParticipantLabel")
        if event == "participant-join" and label in (OWNER, REMOTE):
            try:
                await store.bind_call_sid(session_id, label, call_sid)
            except OperatorRejected as exc:
                raise HTTPException(400, "Conference participant does not match") from exc
            role = label
        state.sid = sid
        identity = (event, call_sid, str(form.get("SequenceNumber", "")))
        if identity in state.events:
            return Response(status_code=204)
        if not session.active:
            return Response(status_code=204)
        if role and event in {"participant-join", "participant-leave"}:
            sequence = str(form.get("SequenceNumber", ""))
            if not sequence.isascii() or not sequence.isdecimal() or len(sequence) > 10:
                raise HTTPException(400, "Invalid conference event sequence")
            previous = state.sequences.get(role, -1)
            pending_retry = (role == OWNER and event == "participant-join"
                             and int(sequence) == previous and state.owner_reconcile_pending)
            if int(sequence) <= previous and not pending_retry:
                return Response(status_code=204)
            state.sequences[role] = int(sequence)
        if event == "participant-join" and role:
            state.participants.add(role)
            if role == OWNER:
                state.owner_reconcile_pending = True
                state.owner_menu = False
                # A mode switch can finish between rejoin TwiML generation and
                # this callback. Reconcile the actual participant on every join.
                async with state.control:
                    muted = session.mode in (ANNOUNCING, AGENT)
                    await controller.native_mutation(controller.dialer.mute_participant(state.sid, call_sid, muted))
                    session.native_owner_muted = muted
                    state.owner_reconcile_pending = False
            await controller.native_connected(session_id)
        elif event == "participant-leave" and role:
            state.participants.discard(role)
            if role == OWNER:
                state.owner_reconcile_pending = False
                session.native_owner_muted = True
            if role == REMOTE:
                await controller.end(session_id, "remote-left-conference")
        elif event == "conference-end":
            await controller.end(session_id, "native-conference-ended")
        if len(state.events) < 2048:
            state.events.add(identity)
        return Response(status_code=204)

    @app.post("/twilio/native-finished/{session_id}/{role}")
    async def native_finished(session_id: str, role: str, form=Depends(validate_twilio)):
        if role != REMOTE:
            raise HTTPException(400, "Unknown native role")
        session = session_for(session_id, form, role)
        if session.active:
            await controller.end(session_id, "native-conference-finished")
        return hangup()

    @app.post("/twilio/native-agent")
    async def agent_app(request: Request, form=Depends(validate_twilio)):
        def param(name):
            return form.get(name) or form.get("Param_" + name) or request.query_params.get(name)
        session_id = param("session_id")
        session = store.find(session_id)
        if not session or not session.active or not session.native_conference:
            return hangup()
        state = controller.native_state(session_id)
        token = param("token")
        if (not isinstance(token, str) or not hmac.compare_digest(token, state.bot_token)
                or str(param("generation")) != str(state.bot_generation)
                or state.bot_task is None or state.bot_token_used
                or time.monotonic() - state.bot_issued > TOKEN_SECONDS):
            raise HTTPException(403, "Invalid native agent binding")
        call_sid = require_sid(form.get("CallSid"))
        if state.bot_application_call and state.bot_application_call != call_sid:
            raise HTTPException(400, "Native app call does not match")
        state.bot_application_call = call_sid
        return xml(native_agent_twiml(settings, session_id, state.bot_generation, state.bot_token))

    @app.post("/twilio/native-agent/status/{session_id}/{generation}")
    async def agent_status(session_id: str, generation: int, form=Depends(validate_twilio)):
        session = session_for(session_id, form)
        state = controller.native_state(session_id)
        sid = require_sid(form.get("CallSid"))
        if generation != state.bot_generation or (state.bot_participant and state.bot_participant != sid):
            raise HTTPException(400, "Native agent callback does not match")
        if form.get("CallStatus") in {"completed", "failed", "busy", "no-answer", "canceled"}:
            state.bot_gone = True
            state.bot_ready.clear()
            if session.active and session.mode != HUMAN:
                await controller._release(session_id)
        return Response(status_code=204)

    @app.websocket("/conference-media/{session_id}/{role}/")
    async def observer(websocket: WebSocket, session_id: str, role: str):
        if (role not in (OWNER, REMOTE) or not controller.is_native(session_id)
                or not store.find(session_id).active or not valid_media_signature(settings, websocket)):
            await websocket.close(code=1008)
            return
        await controller.router(session_id).serve_observer(websocket, role, store)

    @app.websocket("/native-agent-media/{session_id}/")
    async def bot_socket(websocket: WebSocket, session_id: str):
        if (not controller.is_native(session_id) or not store.find(session_id).active
                or controller.native_state(session_id).bot_task is None
                or not valid_media_signature(settings, websocket)):
            await websocket.close(code=1008)
            return
        await controller.router(session_id).serve_bot(websocket)
