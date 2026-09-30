"""In-memory operator sessions: reservations, role binding, and phase deadlines.

The store never dials, never opens a socket, and never calls a provider. Twilio
REST work belongs to ``routes.py`` and audio belongs to ``audio.py``; both hand
this module finished facts (a Call SID, a ``start`` event) and this module
decides whether they may be bound. That separation is what makes the
start-before-REST-result race testable without a phone.

Two counters that look similar are deliberately different: the *transport
generation* changes when a phone call's stream is rebuilt, while the *reply
epoch* changes when a spoken reply is invalidated. Changing a prompt never
reconnects a phone call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import logging
import re
import secrets
import time
import uuid

import phonenumbers

from .codecs import valid_media_format

log = logging.getLogger("uvicorn.error")

ID = re.compile(r"[0-9a-f]{32}\Z")
CALL_SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
STREAM_SID = re.compile(r"MZ[0-9a-fA-F]{32}\Z")
E164 = re.compile(r"\+[1-9][0-9]{7,14}\Z")

OWNER = "owner"
REMOTE = "remote"
ROLES = (OWNER, REMOTE)

# Session phases. ``ENDED`` is terminal: a delayed Twilio callback must never
# reopen a session, only be ignored.
RESERVED = "reserved"
OWNER_RINGING = "owner_ringing"
OWNER_PROMPT = "owner_prompt"
REMOTE_SETUP = "remote_setup"
CONNECTED = "connected"
ENDED = "ended"

LEG_RESERVED = "reserved"
LEG_DIALING = "dialing"
LEG_STARTED = "started"
LEG_RECONNECTING = "reconnecting"
LEG_ENDED = "ended"

HUMAN = "human"
PREPARING = "preparing"
ANNOUNCING = "announcing"
AGENT = "agent"
MODES = (HUMAN, PREPARING, ANNOUNCING, AGENT)

SPEAKERS = ("owner", "remote", "agent")

TERMINAL_STATUSES = frozenset({"completed", "busy", "no-answer", "failed", "canceled"})
CALL_STATUSES = frozenset({"queued", "initiated", "ringing", "in-progress"}) | TERMINAL_STATUSES
STATUS_RANK = {"queued": 0, "initiated": 1, "ringing": 2, "in-progress": 3}

# Phase deadlines in seconds. Twilio's own per-leg ``timeout`` is 25 seconds, so
# the ringing guard adds a five-second buffer for its callback to arrive. The
# setup ceiling bounds the whole dialing chain with one number.
OWNER_RING_SECONDS = 30.0
OWNER_ACCEPT_SECONDS = 20.0
REMOTE_SETUP_SECONDS = 45.0
SETUP_CEILING_SECONDS = 90.0
RECONCILE_SECONDS = 5.0
RECONNECT_LIMIT = 2
RECONNECT_WINDOW_SECONDS = 10.0
TOKEN_SECONDS = SETUP_CEILING_SECONDS

# ``call`` is absent on purpose: its length comes from ``settings.max_call_seconds``.
DEFAULT_SECONDS = {"owner_ring": OWNER_RING_SECONDS, "owner_accept": OWNER_ACCEPT_SECONDS,
                   "remote_setup": REMOTE_SETUP_SECONDS, "setup": SETUP_CEILING_SECONDS,
                   "reconcile": RECONCILE_SECONDS}

MAX_GOAL_CHARS = 300
MAX_TURN_CHARS = 500
MAX_TURNS = 200
MAX_STATUS_TURNS = 40
MAX_MARKS = 64

FRAME_COUNTERS = ("frames_in", "frames_out", "dropped", "gaps", "rejected",
                  "cleared", "dtmf")


class OperatorRejected(Exception):
    """A reservation, binding, or transition the operator service refused."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _redacted(number: str) -> str:
    """Enough of a phone number to identify it locally, never enough to dial."""
    text = str(number or "")
    return f"{text[:5]}…{text[-4:]}" if len(text) > 9 else "…"


def _counters() -> dict[str, int]:
    return dict.fromkeys(FRAME_COUNTERS, 0)


@dataclass
class SessionLeg:
    """One phone call: its reserved identity, its bound stream, and its marks."""

    role: str
    transport: str = "phone"
    destination: str = field(default="", repr=False)
    call_sid: str = field(default="", repr=False)
    stream_sid: str = field(default="", repr=False)
    generation: int = 1
    token: str = field(default="", repr=False)
    token_issued: float = field(default_factory=time.monotonic)
    token_used: bool = False
    state: str = LEG_RESERVED
    attached: bool = False
    answered: bool = False
    uncertain: bool = False
    status: str = ""
    ended: bool = False
    connected_at: float = 0.0
    reconnects: list[float] = field(default_factory=list, repr=False)
    marks: dict[str, str] = field(default_factory=dict, repr=False)
    counters: dict[str, int] = field(default_factory=_counters, repr=False)

    def usable(self) -> bool:
        """Whether a stream may still be bound: reserved, dialing, or reconnecting."""
        return not self.ended and self.state in {LEG_RESERVED, LEG_DIALING, LEG_RECONNECTING}

    def to_status(self) -> dict:
        return {"state": self.state, "generation": self.generation,
                "call_bound": bool(self.call_sid), "stream_bound": bool(self.stream_sid),
                "attached": self.attached, "answered": self.answered, "uncertain": self.uncertain,
                "call_status": self.status, "ended": self.ended,
                "reconnects": len(self.reconnects), "marks": dict(self.marks),
                "counters": dict(self.counters)}


@dataclass
class OperatorSession:
    """One operator conversation: state, deadlines, and attributed turns."""

    id: str
    direction: str = "outbound"
    browser_audio: bool = False
    call_token: str = field(default="", repr=False)
    to: str = field(default="", repr=False)
    goal: str = ""
    voice_id: str = ""
    profile: str = "1"
    mode: str = HUMAN
    reply_epoch: int = 0
    phase: str = RESERVED
    created: float = field(default_factory=time.monotonic)
    created_at: str = ""
    deadline: float | None = None
    ended_at: float | None = None
    ended_reason: str = ""
    turns: list[dict] = field(default_factory=list, repr=False)
    summary: str = ""
    canonical_call_sid: str = ""
    duration_seconds: int | None = None
    agent_name: str = ""
    agent_kind: str = "manual"
    voicemail: bool = False
    voicemail_phase: str = ""
    voicemail_fallback: bool = False
    agent_snapshot: object = field(default=None, repr=False)
    legs: dict[str, SessionLeg] = field(default_factory=dict, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    @property
    def active(self) -> bool:
        return self.phase != ENDED

    def to_status(self) -> dict:
        """A bounded, credential-free view for ``GET /api/sessions/{id}``."""
        now = self.ended_at or time.monotonic()
        return {"id": self.id, "direction": self.direction, "browser_audio": self.browser_audio,
                "to": _redacted(self.to),
                "goal": self.goal[:MAX_GOAL_CHARS], "phase": self.phase, "mode": self.mode,
                "profile": self.profile, "reply_epoch": self.reply_epoch,
                "canonical_call_sid": self.canonical_call_sid, "agent_name": self.agent_name,
                "agent_kind": self.agent_kind, "voicemail": self.voicemail,
                "voicemail_phase": self.voicemail_phase, "voicemail_fallback": self.voicemail_fallback,
                "created_at": self.created_at, "elapsed_ms": int((now - self.created) * 1000),
                "setup_deadline_ms": (int((self.deadline - self.created) * 1000)
                                      if self.deadline is not None else None),
                "ended_reason": self.ended_reason, "voice_ready": bool(self.voice_id),
                "legs": {role: leg.to_status() for role, leg in sorted(self.legs.items())},
                "turns": [dict(turn) for turn in self.turns[-MAX_STATUS_TURNS:]],
                "summary": self.summary[:MAX_TURN_CHARS]}


class OperatorSessions:
    """Reserve, bind, and expire operator sessions; never dial or speak."""

    MAX_SESSIONS = 256
    MAX_ACTIVE = 1          # The first demo is one active session at a time.
    TOMBSTONE_SECONDS = 24 * 60 * 60

    def __init__(self, settings, *, deadlines=None, max_active=None):
        self.settings = settings
        self.sessions: dict[str, OperatorSession] = {}
        self.deadlines = dict(deadlines or {})
        self.max_active = self.MAX_ACTIVE if max_active is None else int(max_active)
        self.draining = False
        self.closed = False
        # The controller installs this to end phone calls; without it a timeout
        # only moves session state.
        self.on_timeout = None
        # The controller installs this to cancel per-session work (its keypad
        # deadlines and the reply it is speaking) whenever a session ends,
        # whatever ended it.
        self.on_end = None
        self._lock = asyncio.Lock()
        self._idempotency: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._timers: dict[tuple[str, str], asyncio.Task] = {}

    # ---------------------------------------------------------------- readiness

    @property
    def ready(self) -> bool:
        """Whether an outbound session could be reserved at all."""
        return bool(self.settings.owner_number and self.settings.twilio_number
                    and self.settings.operator_admin_token and (self.settings.allowed_destinations
                    or self.allowed_countries
                    or getattr(self.settings, "operator_inbound_enabled", False)))

    @property
    def allowed(self) -> frozenset[str]:
        return frozenset(self.settings.allowed_destinations or ())

    @property
    def allowed_countries(self) -> frozenset[str]:
        return frozenset(getattr(self.settings, "allowed_destination_countries", ()) or ())

    def destination_allowed(self, destination: str) -> bool:
        """Apply the explicit dialing policy without treating every +1 as US."""
        if (not E164.fullmatch(destination)
                or destination in {self.settings.owner_number, self.settings.twilio_number}):
            return False
        if destination in self.allowed:
            return True
        if not self.allowed_countries:
            return False
        try:
            number = phonenumbers.parse(destination, None)
        except phonenumbers.NumberParseException:
            return False
        return (phonenumbers.is_valid_number(number)
                and phonenumbers.region_code_for_number(number) in self.allowed_countries)

    @property
    def active_count(self) -> int:
        return sum(session.active for session in self.sessions.values())

    @property
    def pending_count(self) -> int:
        """Outstanding dial/reconcile/cleanup work, excluding deadlines."""
        return sum(not task.done() for task in self._tasks)

    async def set_draining(self, draining):
        async with self._lock:
            self.draining = bool(draining)

    # --------------------------------------------------------------- scheduling

    def spawn(self, coro) -> asyncio.Task:
        """Track one piece of network work so draining can wait for it."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Provider text can contain phone numbers, so only the type is logged.
            log.error("operator task failed type=%s", type(task.exception()).__name__)

    async def wait_idle(self):
        """Drain scheduled work, excluding deadlines; useful in tests."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
            # ``gather`` does not suspend for an already-finished snapshot, so
            # the callbacks that prune ``_tasks`` never get a turn without this.
            await asyncio.sleep(0)

    def _arm(self, session_id: str, key: str, reason: str) -> float:
        """Start the phase deadline ``key``, replacing any earlier one."""
        self._cancel(session_id, key)
        timeout = self.deadlines.get(key, self.deadlines.get(key.split(":", 1)[0]))
        if timeout is None:
            timeout = float(self.settings.max_call_seconds) if key == "call" else DEFAULT_SECONDS[key]
            session = self.sessions.get(session_id)
            if (key == "owner_ring" and session and session.direction == "inbound"
                    and getattr(self.settings, "voicemail_agent_enabled", False)):
                timeout = float(getattr(self.settings, "voicemail_agent_ring_seconds", 10))
        task = asyncio.create_task(self._deadline(session_id, key, float(timeout), reason))
        self._timers[(session_id, key)] = task
        return float(timeout)

    def _cancel(self, session_id: str, key: str):
        task = self._timers.pop((session_id, key), None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _deadline(self, session_id: str, key: str, timeout: float, reason: str):
        try:
            await asyncio.sleep(timeout)
            await self._expire(session_id, key, reason)
        finally:
            if self._timers.get((session_id, key)) is asyncio.current_task():
                self._timers.pop((session_id, key), None)

    async def _expire(self, session_id: str, key: str, reason: str):
        session = self.sessions.get(session_id)
        if session is None or not session.active or not self._still_pending(session, key):
            return
        log.info("operator_deadline session=%s phase=%s reason=%s", session_id, session.phase, reason)
        handler = self.on_timeout
        if handler is None:
            await self.end(session_id, reason)
        else:
            await handler(session, reason)

    @staticmethod
    def _still_pending(session: OperatorSession, key: str) -> bool:
        base, _, role = key.partition(":")
        if base == "owner_ring":
            return session.phase in {RESERVED, OWNER_RINGING}
        if base == "owner_accept":
            return session.phase == OWNER_PROMPT
        if base == "remote_setup":
            return session.phase == REMOTE_SETUP
        if base == "setup":
            return session.phase not in {CONNECTED, ENDED}
        if base == "call":
            return session.phase == CONNECTED
        if base == "reconcile":
            leg = session.legs.get(role)
            return bool(leg and not leg.call_sid and not leg.ended and session.active)
        return False

    # ------------------------------------------------------------- reservations

    async def reserve_outbound(self, to, goal, idempotency_key, *, voice_id="", browser_allowed=True):
        """Reserve both legs and one idempotency key; return ``(session, reused)``."""
        key = str(idempotency_key or "").strip()
        try:
            uuid.UUID(key)
        except (AttributeError, TypeError, ValueError):
            raise OperatorRejected("invalid-idempotency-key") from None
        destination = str(to or "").strip()
        text = str(goal or "").strip()
        async with self._lock:
            if self.closed:
                raise OperatorRejected("shutting-down")
            existing = self._idempotency.get(key)
            if existing and existing in self.sessions:
                session = self.sessions[existing]
                if session.browser_audio and not browser_allowed:
                    raise OperatorRejected("destination-not-allowed")
                return session, True
            if self.draining:
                raise OperatorRejected("draining")
            if not self.ready:
                # No owner number, admin token, or dialing policy means this store
                # must not reserve anything, whichever route asks.
                raise OperatorRejected("not-configured")
            if len(text) > MAX_GOAL_CHARS:
                raise OperatorRejected("goal-too-long")
            browser_audio = bool(destination == self.settings.owner_number
                                 and destination != self.settings.twilio_number)
            if (browser_audio and not browser_allowed) or (not browser_audio
                    and not self.destination_allowed(destination)):
                raise OperatorRejected("destination-not-allowed")
            self._prune()
            if len(self.sessions) >= self.MAX_SESSIONS or self.active_count >= self.max_active:
                raise OperatorRejected("capacity")
            session = OperatorSession(id=uuid.uuid4().hex, direction="outbound", browser_audio=browser_audio,
                                      to=destination, goal=text, voice_id=voice_id)
            session.legs[OWNER] = self._new_leg(OWNER, str(self.settings.owner_number))
            if browser_audio:
                session.legs[OWNER].transport = "browser"
            session.legs[REMOTE] = self._new_leg(REMOTE, destination)
            self.sessions[session.id] = session
            self._idempotency[key] = session.id
            session.deadline = session.created + self._arm(session.id, "setup", "setup-timeout")
            return session, False

    async def reserve_inbound(self, call_sid: str, caller: str, call_token: str = ""):
        """Bind an existing inbound remote call without dialing it again."""
        if not CALL_SID.fullmatch(str(call_sid)) or not E164.fullmatch(str(caller)):
            raise OperatorRejected("invalid-inbound-call")
        async with self._lock:
            for session in self.sessions.values():
                if session.canonical_call_sid == call_sid:
                    return session, True
            if self.closed or self.draining:
                raise OperatorRejected("draining")
            if not (getattr(self.settings, "operator_inbound_enabled", False)
                    and self.settings.owner_number and self.settings.twilio_number
                    and self.settings.operator_admin_token):
                raise OperatorRejected("not-configured")
            self._prune()
            if len(self.sessions) >= self.MAX_SESSIONS or self.active_count >= self.max_active:
                raise OperatorRejected("capacity")
            session = OperatorSession(id=uuid.uuid4().hex, direction="inbound", to=caller,
                                      canonical_call_sid=call_sid, call_token=call_token)
            session.legs[OWNER] = self._new_leg(OWNER, self.settings.owner_number)
            session.legs[REMOTE] = self._new_leg(REMOTE, caller)
            session.legs[REMOTE].call_sid = call_sid
            self.sessions[session.id] = session
            session.deadline = session.created + self._arm(session.id, "setup", "setup-timeout")
            return session, False

    def _new_leg(self, role: str, destination: str) -> SessionLeg:
        return SessionLeg(role=role, destination=destination, generation=1,
                          token=secrets.token_urlsafe(32))

    def _prune(self):
        now = time.monotonic()
        for session_id, session in list(self.sessions.items()):
            if session.ended_at is not None and now - session.ended_at >= self.TOMBSTONE_SECONDS:
                del self.sessions[session_id]
        stale = [key for key, value in self._idempotency.items() if value not in self.sessions]
        for key in stale:
            del self._idempotency[key]

    # ------------------------------------------------------------ state changes

    def find(self, session_id):
        """Look a session up by its public identifier, tolerating junk input."""
        if not isinstance(session_id, str) or not ID.fullmatch(session_id):
            return None
        return self.sessions.get(session_id)

    def _require(self, session_id) -> OperatorSession:
        session = self.find(session_id)
        if session is None:
            raise OperatorRejected("unknown-session")
        return session

    @staticmethod
    def _leg(session: OperatorSession, role) -> SessionLeg:
        leg = session.legs.get(role)
        if leg is None:
            raise OperatorRejected("unknown-role")
        return leg

    async def begin_owner_dial(self, session_id) -> bool:
        """Claim the owner dial exactly once, before the REST call is made."""
        async with self._lock:
            session = self._require(session_id)
            if (session.browser_audio or session.phase != RESERVED
                    or not session.legs[OWNER].usable()):
                return False
            session.phase = OWNER_RINGING
            self._arm(session_id, "owner_ring", "owner-no-answer")
            return True

    async def begin_remote_dial(self, session_id) -> bool:
        """Claim the remote dial exactly once, after the owner accepts."""
        async with self._lock:
            session = self._require(session_id)
            leg = session.legs[REMOTE]
            if session.phase != OWNER_PROMPT or not leg.usable():
                return False
            session.phase = REMOTE_SETUP
            self._cancel(session_id, "owner_accept")
            self._arm(session_id, "remote_setup", "remote-unavailable")
            return True

    async def mark_owner_prompt(self, session_id) -> bool:
        """The owner's stream is up: wait for a bare ``1`` to connect."""
        async with self._lock:
            session = self._require(session_id)
            if session.phase == OWNER_PROMPT:
                return True
            if session.phase not in {RESERVED, OWNER_RINGING}:
                return False
            session.phase = OWNER_PROMPT
            self._cancel(session_id, "owner_ring")
            self._arm(session_id, "owner_accept", "owner-no-acceptance")
            return True

    async def mark_connected(self, session_id) -> bool:
        """Both streams are up: start the call deadline and stop setup timers."""
        async with self._lock:
            session = self._require(session_id)
            if not session.active:
                return False
            if session.phase != CONNECTED:
                session.phase = CONNECTED
                for key in ("setup", "owner_ring", "owner_accept", "remote_setup"):
                    self._cancel(session_id, key)
                session.deadline = time.monotonic() + float(self.settings.max_call_seconds)
                self._arm(session_id, "call", "call-time-limit")
            for leg in session.legs.values():
                leg.connected_at = leg.connected_at or time.monotonic()
            return True

    async def claim_voicemail(self, session_id, reason="owner-no-answer") -> bool:
        """Retire an unanswered owner atomically while preserving the caller.

        An authenticated owner stream or answered status wins the race. Once
        voicemail wins, no late owner callback may reconnect that microphone or
        tear down the caller. The controller cancels the retired phone leg.
        """
        async with self._lock:
            session = self._require(session_id)
            if (not getattr(self.settings, "voicemail_agent_enabled", False)
                    or not session.active or session.direction != "inbound"
                    or session.voicemail or session.phase not in {RESERVED, OWNER_RINGING}):
                return False
            owner, remote = session.legs[OWNER], session.legs[REMOTE]
            if owner.answered or owner.attached or remote.ended:
                return False
            if reason not in {"owner-no-answer", "no-answer", "busy", "failed", "canceled"}:
                return False
            session.voicemail = True
            session.voicemail_phase = "greeting"
            session.agent_kind = "voicemail"
            session.phase = CONNECTED
            owner.ended = True
            owner.state = LEG_ENDED
            owner.token = ""
            owner.token_used = True
            owner.uncertain = False
            for key in ("setup", "owner_ring", "owner_accept", "remote_setup", "reconcile:owner"):
                self._cancel(session_id, key)
            session.deadline = time.monotonic() + float(self.settings.max_call_seconds)
            self._arm(session_id, "call", "call-time-limit")
            remote.connected_at = remote.connected_at or time.monotonic()
            return True

    async def bind_call_sid(self, session_id, role, call_sid, *, duration=None) -> str:
        """Bind or verify the Call SID of one leg; returns ``bound`` or ``matched``.

        A signed ``start`` or status callback can arrive before ``calls.create``
        returns, so the first trustworthy SID wins and the eventual REST result
        must agree with it.
        """
        async with self._lock:
            session = self._require(session_id)
            leg = self._leg(session, role)
            sid = str(call_sid or "")
            if leg.transport != "phone":
                raise OperatorRejected("wrong-transport")
            if not CALL_SID.fullmatch(sid):
                raise OperatorRejected("invalid-call-sid")
            if session.voicemail and role == OWNER:
                if leg.call_sid and leg.call_sid != sid:
                    raise OperatorRejected("call-sid-mismatch")
                leg.call_sid = sid
                raise OperatorRejected("voicemail-owner-retired")
            if not session.active or leg.ended:
                raise OperatorRejected("session-ended")
            if leg.call_sid:
                if leg.call_sid != sid:
                    raise OperatorRejected("call-sid-mismatch")
                return "matched"
            leg.call_sid = sid
            leg.uncertain = False
            if leg.state == LEG_RESERVED:
                leg.state = LEG_DIALING
            self._cancel(session_id, f"reconcile:{role}")
            return "bound"

    async def dial_failed(self, session_id, role, reason="dial-failed") -> bool:
        """An uncertain dial outcome: reconcile from callbacks before ending."""
        async with self._lock:
            session = self._require(session_id)
            leg = self._leg(session, role)
            if not session.active or leg.ended or leg.call_sid:
                return False
            leg.uncertain = True
            self._arm(session_id, f"reconcile:{role}", f"{role}-{reason}")
            return True

    async def bind_stream(self, session_id, role, start) -> SessionLeg:
        """Authenticate Twilio's ``start`` and take the one socket slot for it."""
        async with self._lock:
            session = self._require(session_id)
            leg = self._leg(session, role)
            if leg.transport != "phone":
                raise OperatorRejected("wrong-transport")
            if not session.active or leg.ended:
                raise OperatorRejected("session-ended")
            if not isinstance(start, Mapping):
                raise OperatorRejected("invalid-start")
            params = start.get("customParameters")
            params = params if isinstance(params, Mapping) else {}
            account = start.get("accountSid")
            call_sid = start.get("callSid")
            stream_sid = start.get("streamSid")
            generation = params.get("generation")
            token = params.get("token")
            if account != self.settings.account_sid:
                raise OperatorRejected("account-mismatch")
            if not isinstance(call_sid, str) or not CALL_SID.fullmatch(call_sid):
                raise OperatorRejected("invalid-call-sid")
            if not isinstance(stream_sid, str) or not STREAM_SID.fullmatch(stream_sid):
                raise OperatorRejected("invalid-stream-sid")
            # A voice provider can hand back PCM or a different sample rate; only
            # raw mono 8 kHz μ-law keeps a frame at exactly 20 ms.
            if not valid_media_format(start.get("mediaFormat")):
                raise OperatorRejected("unsupported-media-format")
            if not isinstance(token, str) or not hmac.compare_digest(token, leg.token):
                raise OperatorRejected("token-mismatch")
            if time.monotonic() - leg.token_issued > TOKEN_SECONDS:
                raise OperatorRejected("expired-token")
            if (not isinstance(generation, str) or not generation.isdecimal()
                    or int(generation) != leg.generation):
                raise OperatorRejected("stale-generation")
            if leg.attached:
                raise OperatorRejected("duplicate-binding")
            if leg.call_sid and leg.call_sid != call_sid:
                raise OperatorRejected("call-sid-mismatch")
            leg.call_sid = leg.call_sid or call_sid
            leg.stream_sid = stream_sid
            leg.token_used = True
            leg.attached = True
            leg.uncertain = False
            leg.state = LEG_STARTED
            self._cancel(session_id, f"reconcile:{role}")
            return leg

    async def issue_browser_token(self, session_id) -> str:
        """Only the authenticated browser-token route can mint this credential."""
        async with self._lock:
            session = self._require(session_id)
            leg = session.legs[OWNER]
            if not session.active or not session.browser_audio:
                raise OperatorRejected("browser-audio-unavailable")
            if leg.attached or leg.token_used:
                raise OperatorRejected("browser-already-attached")
            leg.token = secrets.token_urlsafe(32)
            leg.token_issued = time.monotonic()
            return leg.token

    async def bind_browser(self, session_id, token) -> SessionLeg:
        """Claim the browser microphone once, without inventing a phone call SID."""
        async with self._lock:
            session = self._require(session_id)
            leg = session.legs[OWNER]
            if not session.active or not session.browser_audio or leg.ended:
                raise OperatorRejected("browser-audio-unavailable")
            if not isinstance(token, str) or not token.isascii() or not hmac.compare_digest(token, leg.token):
                raise OperatorRejected("token-mismatch")
            if time.monotonic() - leg.token_issued > TOKEN_SECONDS:
                raise OperatorRejected("expired-token")
            if leg.attached or leg.token_used:
                raise OperatorRejected("duplicate-binding")
            leg.token_used = True
            leg.attached = leg.answered = True
            leg.state = LEG_STARTED
            leg.stream_sid = "browser-" + session.id
            return leg

    async def detach_socket(self, session_id, role, generation) -> bool:
        """Release the socket slot after its reader loop ends."""
        async with self._lock:
            session = self.find(session_id)
            leg = session.legs.get(role) if session else None
            if leg is None or leg.generation != generation or not leg.attached:
                return False
            leg.attached = False
            return True

    async def rotate_token(self, session_id, role) -> SessionLeg:
        """Issue the next stream generation for a reconnecting leg."""
        async with self._lock:
            session = self._require(session_id)
            leg = self._leg(session, role)
            if not session.active or leg.ended or leg.state not in {LEG_STARTED, LEG_RECONNECTING}:
                raise OperatorRejected("reconnect-unavailable")
            now = time.monotonic()
            leg.reconnects = [at for at in leg.reconnects if now - at < RECONNECT_WINDOW_SECONDS]
            if len(leg.reconnects) >= RECONNECT_LIMIT:
                raise OperatorRejected("reconnect-limit")
            leg.reconnects.append(now)
            leg.generation += 1
            leg.token = secrets.token_urlsafe(32)
            leg.token_issued = now
            leg.token_used = False
            leg.stream_sid = ""
            leg.attached = False
            leg.state = LEG_RECONNECTING
            return leg

    async def record_status(self, session_id, role, call_sid, status, *, duration=None) -> dict:
        """Apply one signed call-state callback to the matching reserved leg."""
        async with self._lock:
            session = self._require(session_id)
            leg = self._leg(session, role)
            if leg.transport != "phone":
                raise OperatorRejected("wrong-transport")
            if not session.active:
                return {"action": "ignored", "reason": session.ended_reason or "ended"}
            raw = str(status or "")
            if raw not in CALL_STATUSES:
                raise OperatorRejected("invalid-status")
            sid = str(call_sid or "")
            if not CALL_SID.fullmatch(sid):
                raise OperatorRejected("invalid-call-sid")
            if leg.call_sid and leg.call_sid != sid:
                raise OperatorRejected("call-sid-mismatch")
            if session.voicemail and role == OWNER:
                leg.call_sid = sid
                leg.status = raw
                return {"action": "retired", "reason": "voicemail-owner-retired",
                        "call_sid": sid, "status": raw}
            if leg.ended:
                return {"action": "ignored", "reason": "leg-ended"}
            if not leg.call_sid:
                leg.call_sid = sid
                leg.uncertain = False
                if leg.state == LEG_RESERVED:
                    leg.state = LEG_DIALING
                self._cancel(session_id, f"reconcile:{role}")
            if raw not in TERMINAL_STATUSES and STATUS_RANK.get(raw, -1) < STATUS_RANK.get(leg.status, -1):
                return {"action": "ignored", "reason": "out-of-order"}
            leg.status = raw
            if raw == "in-progress":
                leg.answered = True
            if raw in TERMINAL_STATUSES:
                leg.ended = True
                leg.state = LEG_ENDED
                if (sid == session.canonical_call_sid and isinstance(duration, int)
                        and not isinstance(duration, bool) and 0 <= duration <= 999999):
                    session.duration_seconds = duration
                return {"action": "terminal", "reason": raw, "duration_seconds": duration}
            return {"action": "accepted", "status": raw}

    async def set_mode(self, session_id, mode) -> bool:
        """Switch between human relay and delegated agent audio."""
        if mode not in MODES:
            raise OperatorRejected("invalid-mode")
        async with self._lock:
            session = self._require(session_id)
            if not session.active:
                raise OperatorRejected("session-ended")
            if session.mode == mode:
                return False        # An already-active profile is a no-op.
            session.mode = mode
            session.reply_epoch += 1
            return True

    async def select_profile(self, session_id, profile, *, mode=AGENT) -> bool:
        """Select one saved profile and delegate the conversation to it.

        An already-active profile is a no-op, so holding ``#1`` does not restart
        speech. Any real change cancels the previous reply by incrementing the
        reply epoch exactly once, which covers a switch that keeps agent mode
        and leaves buffered frames worthless. Changing a prompt never reconnects
        a phone call: only the transport generation does that.
        """
        if not isinstance(profile, str) or not 1 <= len(profile) <= 8:
            raise OperatorRejected("invalid-profile")
        async with self._lock:
            session = self._require(session_id)
            if not session.active:
                raise OperatorRejected("session-ended")
            if session.profile == profile and session.mode in (PREPARING, ANNOUNCING, AGENT):
                return False
            session.profile = profile
            session.mode = mode
            session.reply_epoch += 1
            return True

    async def transition(self, session_id, epoch: int, mode: str) -> bool:
        """Advance one takeover without invalidating its generation."""
        if mode not in MODES:
            raise OperatorRejected("invalid-mode")
        async with self._lock:
            session = self._require(session_id)
            if not session.active or session.reply_epoch != epoch or session.mode == HUMAN:
                return False
            session.mode = mode
            return True

    async def invalidate_reply(self, session_id) -> int:
        async with self._lock:
            session = self._require(session_id)
            session.reply_epoch += 1
            return session.reply_epoch

    def note_mark(self, session_id, role, name, state) -> bool:
        """Record playback-mark state without awaiting: readers must not block."""
        session = self.sessions.get(session_id)
        leg = session.legs.get(role) if session else None
        if leg is None or not isinstance(name, str) or not name:
            return False
        leg.marks[name] = str(state)
        while len(leg.marks) > MAX_MARKS:
            del leg.marks[next(iter(leg.marks))]
        return True

    def mark_state(self, session_id, role, name) -> str:
        """The last recorded state of one playback mark, or an empty string."""
        session = self.sessions.get(session_id)
        leg = session.legs.get(role) if session else None
        return leg.marks.get(name, "") if leg else ""

    async def add_turn(self, session_id, speaker, text):
        """Append one attributed turn, kept in order and bounded in length."""
        if speaker not in SPEAKERS:
            raise OperatorRejected("unknown-speaker")
        async with self._lock:
            session = self._require(session_id)
            if not session.active:
                return None
            turn = {"speaker": speaker, "text": str(text or "")[:MAX_TURN_CHARS],
                    "at_ms": int((time.monotonic() - session.created) * 1000)}
            session.turns.append(turn)
            del session.turns[:-MAX_TURNS]
            return turn

    async def end(self, session_id, reason="ended"):
        """Close the session and return the call legs that still need hanging up."""
        async with self._lock:
            session = self.find(session_id)
            if session is None or not session.active:
                return []
            session.phase = ENDED
            session.ended_at = time.monotonic()
            session.ended_reason = str(reason)[:60]
            for sid, key in list(self._timers):
                if sid == session_id:
                    self._cancel(session_id, key)
            hangup = []
            for role, leg in session.legs.items():
                leg.attached = False
                if not leg.ended:
                    leg.ended = True
                    leg.state = LEG_ENDED
                    if leg.call_sid:
                        hangup.append((role, leg.call_sid))
        await self._notify_end(session_id)
        return hangup

    async def _notify_end(self, session_id: str):
        """Tell the controller a session is over, outside the session lock."""
        handler = self.on_end
        if handler is None:
            return
        try:
            await handler(session_id)
        except Exception as exc:
            log.warning("operator end observer failed type=%s", type(exc).__name__)

    async def close(self):
        self.closed = True
        self.draining = True
        for session_id in list(self.sessions):
            await self.end(session_id, "server-shutdown")
        timers = list(self._timers.values())
        self._timers.clear()
        for task in timers:
            task.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
        try:
            await asyncio.wait_for(self.wait_idle(), timeout=20)
        except TimeoutError:
            log.warning("operator cleanup timed out during shutdown")
