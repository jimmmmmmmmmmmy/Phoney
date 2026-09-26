"""Idempotent conference lifecycle with network work outside webhook handlers."""

import asyncio
from collections.abc import Mapping
import logging
import re
import time

from twilio.base.exceptions import TwilioRestException

from .gateway import TERMINAL_STATUSES, TwilioGateway
from .models import CallSession, SessionRejected

log = logging.getLogger(__name__)
CALL_SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
CONFERENCE_SID = re.compile(r"CF[0-9a-fA-F]{32}\Z")
STATUS_RANK = {"queued": 0, "initiated": 1, "ringing": 2, "in-progress": 3}


class Switchboard:
    # Do not evict recent tombstones to admit new calls: fail closed instead.
    MAX_SESSIONS = 4096
    MAX_ACTIVE = 16
    TOMBSTONE_SECONDS = 24 * 60 * 60

    def __init__(self, settings, gateway=None):
        self.settings = settings
        self.gateway = gateway if gateway is not None else TwilioGateway(settings)
        self.sessions: dict[str, CallSession] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._deadlines: dict[str, asyncio.Task] = {}
        self._closing = False
        self.draining = False

    @property
    def active_count(self):
        return sum(s.phase != "ended" for s in self.sessions.values())

    @property
    def pending_count(self):
        """Outstanding dial/reconciliation/cleanup tasks, excluding deadlines."""
        return sum(not task.done() for task in self._tasks)

    async def set_draining(self, draining):
        async with self._lock:
            self.draining = bool(draining)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Exception text from providers can contain phone numbers or tokens.
            log.error("switchboard task failed type=%s", type(task.exception()).__name__)

    async def start(self, call_sid, from_number=""):
        if not CALL_SID.fullmatch(call_sid):
            raise SessionRejected("invalid_call")
        async with self._lock:
            if self._closing:
                raise SessionRejected("shutting_down")
            existing = self.sessions.get(call_sid)
            if existing:
                return existing
            if self.draining:
                raise SessionRejected("draining")
            if not self.settings.callee_number or not self.settings.twilio_number:
                raise SessionRejected("not_configured")
            if from_number and from_number in {
                self.settings.callee_number, self.settings.twilio_number
            }:
                raise SessionRejected("self_call")
            now = time.monotonic()
            for sid, session in list(self.sessions.items()):
                if session.ended_at is not None and now - session.ended_at >= self.TOMBSTONE_SECONDS:
                    del self.sessions[sid]
            if len(self.sessions) >= self.MAX_SESSIONS or self.active_count >= self.MAX_ACTIVE:
                raise SessionRejected("capacity")
            session = CallSession(call_sid, f"operator-{call_sid}")
            self.sessions[call_sid] = session
            self._deadlines[call_sid] = asyncio.create_task(self._deadline(call_sid))
            return session

    async def conference_event(self, parent_sid, form: Mapping):
        async with self._lock:
            s = self.sessions.get(parent_sid)
            if not s:
                return
            cf_sid = str(form.get("ConferenceSid", ""))
            if (not CONFERENCE_SID.fullmatch(cf_sid)
                    or form.get("FriendlyName") != s.conference_name
                    or (s.conference_sid and s.conference_sid != cf_sid)):
                return
            event = form.get("StatusCallbackEvent", "")
            call_sid = str(form.get("CallSid", ""))
            label = form.get("ParticipantLabel", "")
            if event in {"participant-join", "participant-leave"}:
                if label == "caller":
                    if call_sid != parent_sid:
                        return
                elif label == "callee":
                    if (not s.dial_reserved or not CALL_SID.fullmatch(call_sid)
                            or call_sid == parent_sid
                            or (s.outbound_sid and s.outbound_sid != call_sid)):
                        return
                else:
                    return
            elif event not in {"conference-start", "conference-end"}:
                return
            # A start event may arrive before the caller's join. Accumulate facts
            # rather than discarding every event with a lower sequence number.
            s.conference_sid = cf_sid
            sequence = str(form.get("SequenceNumber", ""))
            identity = (str(event), call_sid, sequence)
            if identity in s.seen_events:
                return
            if len(s.seen_events) >= 128:
                self._end_locked(s, "event_limit")
                return
            s.seen_events.add(identity)
            if sequence.isdigit():
                s.last_event_sequence = max(s.last_event_sequence, int(sequence))
            if s.phase == "ended":
                # A timed-out REST request can complete after reconciliation.
                # Learn that eventual participant without reviving the session.
                if label == "callee":
                    s.outbound_sid = call_sid
                    self._spawn(self._safe_end_call(call_sid))
                if event in {"participant-join", "conference-start"}:
                    self._spawn(self._safe_end_conference(cf_sid))
                return
            if event == "conference-end":
                self._end_locked(s, "conference_ended")
            elif event == "participant-leave":
                if label == "callee":
                    s.outbound_sid = call_sid
                self._end_locked(s, f"{label}_left")
            elif event == "conference-start":
                s.conference_started = True
                self._mark_connected(s)
            elif label == "caller":
                s.caller_joined = True
                if not s.dial_reserved:
                    s.dial_reserved = True
                    s.phase = "dialing"
                    self._spawn(self._dial(s))
                self._mark_connected(s)
            else:
                s.outbound_sid = call_sid
                s.callee_joined = True
                self._apply_pending(s)
                self._mark_connected(s)

    async def call_status(self, parent_sid, form: Mapping):
        async with self._lock:
            s = self.sessions.get(parent_sid)
            call_sid = str(form.get("CallSid", ""))
            status = str(form.get("CallStatus", ""))
            if (not s or not s.dial_reserved or not CALL_SID.fullmatch(call_sid)
                    or call_sid == parent_sid
                    or status not in STATUS_RANK.keys() | TERMINAL_STATUSES):
                return
            if s.outbound_sid and s.outbound_sid != call_sid:
                return
            if not s.outbound_sid:
                verified_endpoints = (
                    form.get("From") == self.settings.twilio_number
                    and form.get("To") == self.settings.callee_number
                    and form.get("Direction") in {"outbound-api", "outbound-dial"}
                )
                if verified_endpoints:
                    s.outbound_sid = call_sid
                else:
                    # An early callback without endpoint evidence is not enough
                    # to claim a SID; await REST or a labeled conference event.
                    if call_sid in s.pending_status or len(s.pending_status) < 8:
                        previous = s.pending_status.get(call_sid, "")
                        if previous not in TERMINAL_STATUSES:
                            s.pending_status[call_sid] = status
                    return
            if s.phase == "ended":
                if status not in TERMINAL_STATUSES:
                    self._spawn(self._safe_end_call(call_sid))
                return
            self._apply_status(s, status)

    def _apply_status(self, s, status):
        if s.phase == "ended":
            return
        if status in TERMINAL_STATUSES:
            s.call_status = status
            self._end_locked(s, status)
        elif STATUS_RANK.get(status, -1) > STATUS_RANK.get(s.call_status, -1):
            s.call_status = status

    def _apply_pending(self, s):
        status = s.pending_status.pop(s.outbound_sid, None)
        s.pending_status.clear()
        if status:
            self._apply_status(s, status)

    def _mark_connected(self, s):
        if s.phase != "ended" and s.caller_joined and s.callee_joined and s.conference_started:
            s.phase = "connected"
            s.connected = True
            self._cancel_deadline(s.parent_sid)

    def _cancel_deadline(self, parent_sid):
        task = self._deadlines.pop(parent_sid, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    async def _deadline(self, parent_sid):
        try:
            await asyncio.sleep(self.settings.switchboard_setup_timeout)
            async with self._lock:
                s = self.sessions.get(parent_sid)
                if s and s.phase not in {"connected", "ended"}:
                    self._end_locked(s, "setup_timeout")
        finally:
            self._deadlines.pop(parent_sid, None)

    async def _dial(self, s):
        # Task may not have started before a leave callback marks the room ended.
        async with self._lock:
            if s.phase == "ended":
                return
        try:
            sid = await self.gateway.create_participant(s.conference_sid, s.parent_sid)
            if not isinstance(sid, str) or not CALL_SID.fullmatch(sid) or sid == s.parent_sid:
                raise ValueError("invalid outbound SID")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("participant create failed parent=%s type=%s", s.parent_sid, type(exc).__name__)
            # No blind retry: a failed HTTP response can hide a successful dial.
            try:
                sid = await self.gateway.find_participant(s.conference_sid, "callee")
            except Exception as lookup_exc:
                log.warning("participant reconciliation failed parent=%s type=%s",
                            s.parent_sid, type(lookup_exc).__name__)
                sid = None
            if not isinstance(sid, str) or not CALL_SID.fullmatch(sid) or sid == s.parent_sid:
                sid = None
            async with self._lock:
                if sid:
                    if not s.outbound_sid:
                        s.outbound_sid = sid
                    elif s.outbound_sid != sid:
                        self._spawn(self._safe_end_call(sid))
                if s.phase == "ended":
                    if sid:
                        self._spawn(self._safe_end_call(sid))
                else:
                    self._end_locked(s, "dial_failed")
            return
        async with self._lock:
            if s.outbound_sid and s.outbound_sid != sid:
                self._spawn(self._safe_end_call(sid))
                self._end_locked(s, "outbound_identity_mismatch")
            else:
                s.outbound_sid = sid
                self._apply_pending(s)
                if s.phase == "ended":
                    self._spawn(self._safe_end_call(sid))

    def _end_locked(self, s, reason):
        if s.phase == "ended":
            return
        s.phase = "ended"
        s.reason = reason
        s.ended_at = time.monotonic()
        self._cancel_deadline(s.parent_sid)
        self._spawn(self._cleanup(s))
        log.info("switchboard ended parent=%s reason=%s", s.parent_sid, reason)

    async def _safe_end_call(self, sid):
        await self._cleanup_request(self.gateway.end_call, "call", sid)

    async def _safe_end_conference(self, sid):
        await self._cleanup_request(self.gateway.end_conference, "conference", sid)

    async def _cleanup_request(self, operation, kind, sid):
        # Terminating a call is idempotent; creating a call is not. Only cleanup
        # has a retry, so a transient provider failure cannot cause a redial.
        for attempt in range(2):
            try:
                await operation(sid)
                return
            except Exception as exc:
                permanent = isinstance(exc, TwilioRestException) and 400 <= exc.status < 500 and exc.status != 429
                if attempt or permanent:
                    log.warning("%s cleanup failed sid=%s type=%s", kind, sid, type(exc).__name__)
                    return
                await asyncio.sleep(0.25)

    async def _cleanup(self, s):
        work = [self._safe_end_call(s.parent_sid)]
        if s.conference_sid:
            work.append(self._safe_end_conference(s.conference_sid))
        if s.outbound_sid:
            work.append(self._safe_end_call(s.outbound_sid))
        await asyncio.gather(*work)

    async def finished(self, parent_sid):
        async with self._lock:
            s = self.sessions.get(parent_sid)
            if s:
                self._end_locked(s, "caller_finished")

    async def wait_idle(self):
        """Drain scheduled work, excluding setup deadlines; useful in tests."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def close(self):
        async with self._lock:
            self._closing = True
            self.draining = True
            for s in self.sessions.values():
                self._end_locked(s, "server_shutdown")
            deadlines = tuple(self._deadlines.values())
            self._deadlines.clear()
            for task in deadlines:
                task.cancel()
        await asyncio.gather(*deadlines, return_exceptions=True)
        # SDK requests have 10-second timeouts. Allow create/reconcile and final
        # cleanup to settle before shutdown cancels outstanding asyncio tasks.
        try:
            await asyncio.wait_for(self.wait_idle(), timeout=35)
        except TimeoutError:
            tasks = tuple(self._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
