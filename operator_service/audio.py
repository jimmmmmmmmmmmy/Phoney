"""Two-way audio between one owner leg and one remote leg.

One reader per Twilio socket and one paced writer per output socket. Readers
dispatch control messages immediately and never wait on a provider; writers
serialize media, marks, and ``clear`` on a monotonic 20 ms clock. ``clear``
outranks audio, so a mode change cannot be preceded by stale speech.

The bridge exists to keep two people talking, so the buffers are deliberately
tiny: live audio is capped at 200 ms and old frames are discarded instead of
being replayed late. The only delay this module adds on purpose is a two-frame
jitter buffer (40 ms) when a writer anchors its clock.
"""

from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import time
from typing import Protocol

from voice_stack.audio import FRAME_BYTES, FRAME_MS
from voice_stack.audio import iter_frames

from .codecs import (SILENCE_FRAME, clear_message, inbound_frames, mark_message,
                     media_message, mix_ulaw, ringback_pattern)
from .sessions import HUMAN, PREPARING, OWNER, REMOTE, ROLES, OperatorRejected

log = logging.getLogger("uvicorn.error")

LIVE_FRAMES = 10            # 200 ms of human speech, then the oldest audio goes.
AGENT_FRAMES = 50           # One second of buffered agent speech, then backpressure.
UNDERFLOW_FRAMES = 4        # 80 ms of silence smooths a short scheduler stall.
JITTER_FRAMES = 2           # 40 ms of slack before a writer anchors its clock.
CLOCK_DRIFT_SECONDS = 0.25  # Never burst a backlog after an event-loop stall.
START_SECONDS = 5.0         # Twilio's ``start`` must authenticate within five seconds.
MAX_MESSAGE_BYTES = 65_536
MAX_BAD_MESSAGES = 10
MAX_SEEN_KEYS = 512
FRAME_SECONDS = FRAME_MS / 1000
DIGITS = frozenset("0123456789*#")


class LegController(Protocol):
    """What a router needs from its controller; none of it calls a provider."""

    async def stream_started(self, session_id: str, role: str, stream_sid: str) -> None: ...

    async def stream_stopped(self, session_id: str, role: str, reason: str) -> None: ...

    async def dtmf(self, session_id: str, digit: str) -> None: ...

    async def mark(self, session_id: str, role: str, name: str, state: str) -> None: ...

    def on_audio(self, session_id: str, role: str, frame: bytes) -> None: ...


class _SeenKeys:
    """Bounded ``(stream, sequence)`` memory: repeated digits can be intentional."""

    def __init__(self, limit: int = MAX_SEEN_KEYS):
        self.limit = limit
        self._order: deque = deque()
        self._seen: set = set()

    def first(self, key) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        self._order.append(key)
        while len(self._order) > self.limit:
            self._seen.discard(self._order.popleft())
        return True


class QueuedFrame(bytes):
    """Keep provenance beside audio without changing its byte representation."""
    def __new__(cls, frame, kind, reply_epoch=None):
        value = super().__new__(cls, frame)
        value.kind = kind
        value.reply_epoch = getattr(frame, "reply_epoch", None) if reply_epoch is None else reply_epoch
        return value


class OutputChannel:
    """The paced writer for one leg's outgoing Twilio stream."""

    def __init__(self, role: str):
        self.role = role
        self.media: deque[bytes] = deque()
        self.agent: deque[bytes] = deque()
        self.marks: deque[str] = deque()
        self.counters: dict[str, int] = {}
        self.pending_clear = False
        self.underflow = UNDERFLOW_FRAMES
        self.stream_sid = ""
        self.generation = 0
        self._socket = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self.on_sent = None
        self._last_frame = None
        self._last_kind = "silence"

    @property
    def attached(self) -> bool:
        return self._socket is not None

    def attach(self, websocket, stream_sid: str, generation: int, counters: dict):
        """Bind this channel to a socket and start its writer."""
        self.detach()
        self._socket = websocket
        self.stream_sid = stream_sid
        self.generation = generation
        self.counters = counters
        self.underflow = UNDERFLOW_FRAMES
        self.pending_clear = False
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._write())

    def detach(self):
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        self._socket = None
        self.stream_sid = ""
        self.generation = 0
        self.media.clear()
        self.agent.clear()
        self.marks.clear()
        self.pending_clear = False

    # ------------------------------------------------------------------ sending

    def send(self, frame: bytes, *, kind: str = "live", reply_epoch=None) -> bool:
        """Queue audio for this leg; drop it when the leg has no stream."""
        if not self.attached:
            self._count("dropped")
            return False
        cap = AGENT_FRAMES if kind == "agent" else LIVE_FRAMES
        self._push(self.media, QueuedFrame(frame, kind, reply_epoch), cap)
        self.underflow = UNDERFLOW_FRAMES
        self._wake.set()
        return True

    def send_agent(self, frame: bytes, *, reply_epoch=None) -> bool:
        """Queue agent speech for the owner's monitor mix."""
        if not self.attached:
            self._count("dropped")
            return False
        self._push(self.agent, QueuedFrame(frame, "agent", reply_epoch), AGENT_FRAMES)
        self.underflow = UNDERFLOW_FRAMES
        self._wake.set()
        return True

    def mark(self, name: str) -> bool:
        if not self.attached:
            return False
        self.marks.append(name)
        self._wake.set()
        return True

    def clear(self):
        """Discard queued speech and emit Twilio ``clear``; returns invalidated marks."""
        dropped = len(self.media) + len(self.agent)
        self._count("cleared", len(self.media))
        self._count("dropped", len(self.agent))
        self.media.clear()
        self.agent.clear()
        invalidated = list(self.marks)
        self.marks.clear()
        self.pending_clear = True
        self.underflow = UNDERFLOW_FRAMES
        self._wake.set()
        return dropped, invalidated

    def _push(self, queue: deque, frame: bytes, cap: int):
        while len(queue) >= cap:
            queue.popleft()
            self._count("dropped")
            self._count("gaps")
        queue.append(frame)

    def _count(self, name: str, amount: int = 1):
        if self.counters:
            self.counters[name] = self.counters.get(name, 0) + amount

    # ------------------------------------------------------------------ writing

    async def _write(self):
        socket = self._socket
        deadline = None
        try:
            while True:
                self._wake.clear()
                message = self._take()
                if message is None:
                    # Nothing to say: stay quiet instead of streaming silence
                    # for the rest of the call, and abandon the old clock.
                    await self._wake.wait()
                    deadline = None
                    continue
                sent_frame, sent_kind = self._last_frame, self._last_kind
                await socket.send_json(message)
                if message["event"] == "media":
                    self._count("frames_out")
                    if self.on_sent is not None and sent_frame is not None:
                        try:
                            self.on_sent(sent_frame, sent_kind)
                        except Exception as exc:
                            log.warning("operator_output_observer_failed type=%s", type(exc).__name__)
                if deadline is None:
                    deadline = time.monotonic() + JITTER_FRAMES * FRAME_SECONDS
                deadline += FRAME_SECONDS
                delay = deadline - time.monotonic()
                if delay <= -CLOCK_DRIFT_SECONDS:
                    self._count("gaps")
                    deadline = time.monotonic()
                    continue
                if delay > 0:
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a closed socket must not crash the session
            log.warning("operator_writer_stopped role=%s type=%s", self.role, type(exc).__name__)

    def _take(self):
        """The next message in priority order, or ``None`` once there is nothing."""
        if self.pending_clear:
            self.pending_clear = False
            return clear_message(self.stream_sid)
        if self.media:
            frame = self.media.popleft()
            kind = getattr(frame, "kind", "live")
            if self.agent:
                frame = mix_ulaw(frame, self.agent.popleft())
            self._last_frame, self._last_kind = frame, kind
            return media_message(self.stream_sid, frame)
        if self.agent:
            frame = self.agent.popleft()
            self._last_frame, self._last_kind = frame, "agent"
            return media_message(self.stream_sid, frame)
        if self.marks:
            # A mark follows every frame queued before it, so playback of that
            # phrase is confirmed only for audio that was actually sent.
            return mark_message(self.stream_sid, self.marks.popleft())
        if self.underflow > 0:
            self.underflow -= 1
            self._last_frame, self._last_kind = SILENCE_FRAME, "silence"
            return media_message(self.stream_sid, SILENCE_FRAME)
        return None


class CallRouter:
    """Session audio: reads both legs' sockets and forwards between them."""

    def __init__(self, session_id: str, controller: LegController):
        self.session_id = session_id
        self.controller = controller
        self.channels = {OWNER: OutputChannel(OWNER), REMOTE: OutputChannel(REMOTE)}
        self.generations = {OWNER: 0, REMOTE: 0}
        self.mode = HUMAN
        self.owner_notice = False
        self.closed = False
        self.browser_socket = None
        self.cue: asyncio.Task | None = None
        self.counters = {"routed": 0, "owner_muted": 0, "dtmf_ignored": 0,
                         "rejected_messages": 0, "bad_marks": 0, "marks_played": 0}
        self._dtmf_seen = _SeenKeys()
        self._source_offsets = {}
        self._source_last = {}
        self.channels[REMOTE].on_sent = self._output_sent

    def _output_sent(self, frame, kind):
        observer = getattr(self.controller, "output_audio", None)
        if observer is not None:
            observer(self.session_id, frame, "human" if kind == "live" else kind)

    def attached(self, role: str) -> bool:
        return self.channels[role].attached

    def pending_agent(self, role: str) -> int:
        """Agent frames queued ahead of the writer: a producer's backpressure."""
        channel = self.channels[role]
        return len(channel.media if role == REMOTE else channel.agent)

    # ------------------------------------------------------------------ routing

    def forward(self, source: str, frame: bytes, timestamp_ms=None):
        """Send one speaker's frame to the other leg; returns whether it landed."""
        relay_ready = self.controller.relay_ready(self.session_id)
        if source == OWNER and not relay_ready:
            return False
        if source == OWNER and self.mode not in (HUMAN, PREPARING):
            # While delegated the owner's microphone is neither forwarded nor
            # transcribed; they keep listening and using the keypad.
            self.counters["owner_muted"] += 1
            return False
        destination = REMOTE if source == OWNER else OWNER
        # The caller can be captured while waiting, but neither microphone is
        # sent to the other phone until the owner has accepted the call.
        delivered = (relay_ready and not (destination == OWNER and self.owner_notice)
                     and self.channels[destination].send(frame))
        if delivered:
            self.counters["routed"] += 1
        if timestamp_ms is None:
            self.controller.on_audio(self.session_id, source, frame)
        else:
            self.controller.on_audio(self.session_id, source, frame, timestamp_ms)
        return delivered

    def send_agent(self, frames, *, reply_epoch=None):
        """Send cloned speech to the remote and mix it into the owner's monitor."""
        sent = 0
        for frame in frames:
            if not isinstance(frame, bytes) or len(frame) != FRAME_BYTES:
                raise ValueError("Agent audio must be complete μ-law frames.")
            if self.channels[REMOTE].send(frame, kind="agent", reply_epoch=reply_epoch):
                sent += 1
            if self.channels[OWNER].attached:
                self.channels[OWNER].send_agent(frame, reply_epoch=reply_epoch)
        return sent

    def send_announcement(self, frame, *, role=REMOTE):
        """Deliver a disclosure or private owner notice to exactly one leg."""
        if role == OWNER:
            return self.channels[OWNER].send_agent(frame)
        return self.channels[REMOTE].send(frame, kind="announcement")

    def set_owner_notice(self, active):
        self.owner_notice = bool(active)
        if active:
            return self.clear(OWNER)


    def clear(self, *roles):
        """Drop queued speech on the named outputs and record invalidated marks."""
        invalidated = []
        dropped = 0
        for role in roles or ROLES:
            count, names = self.channels[role].clear()
            dropped += count
            invalidated.extend((role, name) for name in names)
        return {"dropped": dropped, "marks": invalidated}

    def set_mode(self, mode: str):
        """Change who is speaking; buffered speech from the old role goes first."""
        if mode == HUMAN:
            self.owner_notice = False
        if mode == self.mode:
            return None
        previous = self.mode
        self.mode = mode
        if previous == HUMAN and mode == PREPARING:
            return None  # Provider preparation must not interrupt human relay.
        if mode in (HUMAN, PREPARING):
            return self.clear(*ROLES)
        return self.clear(REMOTE)

    async def mark(self, role: str, name: str) -> bool:
        """Label the end of a spoken phrase so playback can be confirmed later."""
        if not self.channels[role].mark(name):
            return False
        await self._dispatch_control(self.controller.mark(self.session_id, role, name, "pending"))
        return True

    # -------------------------------------------------------------------- cues

    def start_cue(self, role: str = OWNER, pattern: bytes | None = None):
        """Repeat a private cue to one leg until ``stop_cue`` (ringing, waiting)."""
        self.stop_cue()
        frames = tuple(iter_frames(ringback_pattern() if pattern is None else pattern))
        self.cue = asyncio.create_task(self._cue_loop(role, frames))

    def stop_cue(self):
        task, self.cue = self.cue, None
        if task is not None and not task.done():
            task.cancel()

    async def _cue_loop(self, role: str, frames):
        channel = self.channels[role]
        while True:
            for frame in frames:
                if not channel.attached:
                    # The owner's stream ended; a cue has nowhere to go.
                    return
                channel.send(frame, kind="announcement")
                await asyncio.sleep(FRAME_SECONDS)

    def close(self):
        self.closed = True
        self.stop_cue()
        socket, self.browser_socket = self.browser_socket, None
        if socket is not None:
            self.controller.store.spawn(self._close_browser(socket))
        for channel in self.channels.values():
            channel.detach()

    @staticmethod
    async def _close_browser(socket):
        try:
            await socket.close(code=1000)
        except (RuntimeError, OSError):
            pass

    # ------------------------------------------------------------------ reading

    async def serve_browser(self, websocket, store):
        """A token-bound browser owns the owner channel; it is never a PSTN leg."""
        if self.closed:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        leg = None
        started = time.monotonic()
        bad = 0
        try:
            while not self.closed:
                message = await self._receive(websocket, leg, started)
                if message is None:
                    await websocket.close(code=1008)
                    break
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text")
                event = None
                if isinstance(raw, str) and len(raw.encode("utf-8")) <= MAX_MESSAGE_BYTES:
                    try:
                        event = json.loads(raw)
                    except (ValueError, TypeError):
                        pass
                if not isinstance(event, dict):
                    bad += 1
                elif leg is None:
                    if event.get("event") != "start":
                        await websocket.close(code=1008)
                        break
                    try:
                        leg = await store.bind_browser(self.session_id, event.get("token"))
                    except OperatorRejected:
                        await websocket.close(code=1008)
                        break
                    self.browser_socket = websocket
                    self.generations[OWNER] = leg.generation
                    await websocket.send_json({"event": "ready"})
                    self.channels[OWNER].attach(websocket, leg.stream_sid, leg.generation, leg.counters)
                    await self._dispatch_control(self.controller.stream_started(
                        self.session_id, OWNER, leg.stream_sid))
                    continue
                elif event.get("event") == "stop":
                    break
                elif event.get("event") in {"media", "mark", "dtmf"}:
                    bad += await self._dispatch(OWNER, leg, event)
                else:
                    bad += 1
                if bad >= MAX_BAD_MESSAGES:
                    await websocket.close(code=1008)
                    break
        except (RuntimeError, OSError):
            pass
        finally:
            # A rejected second socket must never detach the authenticated one.
            if leg is not None:
                if self.browser_socket is websocket:
                    self.browser_socket = None
                self.channels[OWNER].detach()
                await store.detach_socket(self.session_id, OWNER, leg.generation)
                await self._dispatch_control(self.controller.stream_stopped(
                    self.session_id, OWNER, "browser-disconnected"))
                await self._close_browser(websocket)

    async def serve(self, websocket, role: str, store):
        """Own one Twilio socket from upgrade to disconnect."""
        if self.closed or role not in ROLES:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        leg = None
        reason = "socket-disconnected"
        bad = 0
        started = time.monotonic()
        try:
            while True:
                message = await self._receive(websocket, leg, started)
                if message is None:
                    reason = "start-timeout"
                    break
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text")
                if (not isinstance(raw, str) or not raw
                        or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES):
                    bad += 1
                else:
                    event = None
                    try:
                        event = json.loads(raw)
                        if not isinstance(event, dict):
                            raise ValueError("Expected an object.")
                    except (TypeError, ValueError):
                        event = None
                    if event is not None:
                        if event.get("event") == "start" and leg is None:
                            try:
                                leg = await store.bind_stream(self.session_id, role,
                                                              event.get("start") or {})
                            except OperatorRejected as exc:
                                log.warning("operator_start_rejected session=%s role=%s reason=%s",
                                            self.session_id, role, exc.reason)
                                reason = "rejected-" + exc.reason
                                await websocket.close(code=1008)
                                return
                            self.generations[role] = leg.generation
                            self.channels[role].attach(websocket, leg.stream_sid,
                                                       leg.generation, leg.counters)
                            await self._dispatch_control(self.controller.stream_started(
                                self.session_id, role, leg.stream_sid))
                            continue
                        bad += await self._dispatch(role, leg, event)
                    else:
                        bad += 1
                self.counters["rejected_messages"] = bad
                if bad >= MAX_BAD_MESSAGES:
                    reason = "invalid-message"
                    break
        except (RuntimeError, OSError):
            reason = "socket-disconnected"
        finally:
            generation = leg.generation if leg else 0
            self.channels[role].detach()
            await store.detach_socket(self.session_id, role, generation)
            await self._dispatch_control(self.controller.stream_stopped(
                self.session_id, role, reason))

    @staticmethod
    async def _receive(websocket, leg, started):
        """Wait for one message, bounding how long an unauthenticated socket lives."""
        if leg is not None:
            return await websocket.receive()
        remaining = START_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            return None
        try:
            return await asyncio.wait_for(websocket.receive(), timeout=remaining)
        except (asyncio.TimeoutError, TimeoutError):
            return None

    async def _dispatch(self, role: str, leg, event: dict) -> int:
        """Route one authenticated message; returns the new bad-message count."""
        kind = event.get("event")
        if kind == "connected":
            return 0
        if leg is None:
            # Only ``connected`` may precede the authenticated ``start``.
            return 1
        if kind == "media":
            try:
                frames = inbound_frames(event)
            except ValueError:
                leg.counters["rejected"] += 1
                return 1
            raw_ms = event.get("media", {}).get("timestamp", "0")
            try:
                raw_ms = max(0, int(raw_ms))
            except (ValueError, TypeError):
                return 1
            clock = getattr(self.controller, "elapsed_ms", None)
            elapsed = clock(self.session_id) if clock else raw_ms
            key = (role, leg.generation)
            if key not in self._source_offsets:
                self._source_offsets[key] = max(elapsed, self._source_last.get(role, -20) + 20) - raw_ms
            timestamp_ms = raw_ms + self._source_offsets[key]
            for index, frame in enumerate(frames):
                leg.counters["frames_in"] += 1
                stamp = max(timestamp_ms + index * FRAME_MS, self._source_last.get(role, -FRAME_MS) + FRAME_MS)
                self._source_last[role] = stamp
                self.forward(role, frame, stamp if clock else None)
            return 0
        if kind == "mark":
            await self._mark_played(role, event)
            return 0
        if kind == "dtmf":
            await self._queue_dtmf(role, leg, event)
            return 0
        if kind == "stop":
            # A stream can stop while its phone call stays up (reconnects, IVR
            # playback); the session ends from call status, not from this.
            return 0
        return 1

    async def _mark_played(self, role: str, event: dict):
        payload = event.get("mark")
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not name:
            self.counters["bad_marks"] += 1
            return
        self.counters["marks_played"] += 1
        await self._dispatch_control(self.controller.mark(self.session_id, role, name, "played"))

    async def _queue_dtmf(self, role: str, leg, event: dict):
        """Only the bound owner leg may change anything, and only once per key event."""
        if role != OWNER or self.generations[OWNER] != leg.generation:
            self.counters["dtmf_ignored"] += 1
            return
        payload = event.get("dtmf")
        digit = payload.get("digit") if isinstance(payload, dict) else None
        if not isinstance(digit, str) or len(digit) != 1 or digit not in DIGITS:
            leg.counters["rejected"] += 1
            return
        key = (str(event.get("streamSid", leg.stream_sid)), str(event.get("sequenceNumber", "")))
        if not self._dtmf_seen.first(key):
            self.counters["dtmf_ignored"] += 1
            return
        leg.counters["dtmf"] += 1
        await self._dispatch_control(self.controller.dtmf(self.session_id, digit))

    @staticmethod
    async def _dispatch_control(coro):
        """Await one bounded control callback; a provider call belongs in a task.

        Everything the controller does from the reader is state bookkeeping or
        scheduling, so the reader keeps its place in the audio stream while the
        dial, the prompt check, or the mode change happens elsewhere.
        """
        try:
            await coro
        except OperatorRejected as exc:
            log.warning("operator_control_rejected reason=%s", exc.reason)
        except Exception as exc:
            log.error("operator_control_failed type=%s", type(exc).__name__)
