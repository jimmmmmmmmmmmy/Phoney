"""Non-blocking bounded input queue for live Modulate shadow detection."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
import math
from typing import AsyncIterator, Callable

from integrations.contracts import AudioFrame
from media_capture.capture import decode_mulaw
from .modulate import (Connector, DEFAULT_DEADLINE_SECONDS, DetectionReport,
                       MAX_AUDIO_SECONDS, stream_inbound_pcm)
from .policy import DetectionDecision, decide_call_detection


_END = object()


@dataclass(frozen=True, slots=True)
class LiveDetectionOutcome:
    """Provider report plus bounded-queue delivery counters."""

    report: DetectionReport
    accepted_frames: int
    dropped_frames: int


class LiveDetectionWorker:
    """Accept live inbound frames without applying backpressure to telephony."""

    def __init__(self, *, api_key: str, queue_frames: int,
                 deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
                 max_audio_seconds: int = MAX_AUDIO_SECONDS,
                 connector: Connector | None = None) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if type(queue_frames) is not int or queue_frames < 1:
            raise ValueError("queue_frames must be a positive integer")
        self._api_key = api_key
        self._queue: asyncio.Queue[AudioFrame | object] = asyncio.Queue(maxsize=queue_frames)
        self._deadline_seconds = deadline_seconds
        self._max_audio_seconds = max_audio_seconds
        self._connector = connector
        self._accepted_frames = 0
        self._dropped_frames = 0
        self._finished = False
        self._started = False
        self.finish_reason: str | None = None
        self.coverage_limited = False

    @property
    def queued_frames(self) -> int:
        return self._queue.qsize()

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    def offer(self, frame: AudioFrame) -> bool:
        """Queue one frame immediately, returning false instead of blocking."""
        if self._finished or frame.track != "inbound":
            return False
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._dropped_frames += 1
            return False
        self._accepted_frames += 1
        return True

    def finish(self, reason: str | None = None, *, coverage_limited: bool = False) -> None:
        """Stop accepting frames and guarantee a non-blocking end marker."""
        self.coverage_limited = self.coverage_limited or coverage_limited
        if self._finished:
            return
        self._finished = True
        self.finish_reason = reason
        try:
            self._queue.put_nowait(_END)
        except asyncio.QueueFull:
            # The reader exits when the already accepted frames are drained.
            pass

    async def _frames(self) -> AsyncIterator[AudioFrame]:
        while True:
            if self._finished and self._queue.empty():
                return
            item = await self._queue.get()
            if item is _END:
                return
            if isinstance(item, AudioFrame):
                yield item

    def _stop_input(self) -> None:
        self._finished = True
        while not self._queue.empty():
            self._queue.get_nowait()

    async def run(self) -> LiveDetectionOutcome:
        """Drain queued frames into one isolated provider stream."""
        if self._started:
            raise RuntimeError("live detection worker can only run once")
        self._started = True
        try:
            report = await stream_inbound_pcm(
                self._frames(), api_key=self._api_key,
                deadline_seconds=self._deadline_seconds,
                collection_seconds=self._max_audio_seconds + 15,
                on_input_complete=self._stop_input,
                max_audio_seconds=self._max_audio_seconds,
                connector=self._connector,
            )
            if self.coverage_limited:
                report = replace(report, coverage_limited=True)
            if self.finish_reason and not report.reason:
                report = replace(report, status="unknown", observations=(), reason=self.finish_reason)
            return LiveDetectionOutcome(report, self._accepted_frames, self._dropped_frames)
        finally:
            self._finished = True
            # Release queued PCM when the cap, a provider failure, or cancellation ends a stream.
            while not self._queue.empty():
                self._queue.get_nowait()


@dataclass(slots=True)
class _LiveSession:
    stream_id: str
    worker: LiveDetectionWorker
    task: asyncio.Task[LiveDetectionOutcome]


class LiveDetectionManager:
    """Keep provider work isolated from calls, with bounded advisory history."""

    MAX_ACTIVE_STREAMS = 32
    MAX_HISTORY_CALLS = 128
    MAX_STREAMS_PER_CALL = 8

    def __init__(self, settings, *, connector: Connector | None = None,
                 shutdown_seconds: float = 5.0,
                 can_run: Callable[[], bool] | None = None,
                 on_update: Callable[[str, dict[str, object]], None] | None = None) -> None:
        if (not isinstance(shutdown_seconds, (int, float))
                or isinstance(shutdown_seconds, bool) or not 0 < shutdown_seconds <= 10):
            raise ValueError("shutdown_seconds must be between 0 and 10")
        self.enabled = bool(getattr(settings, "modulate_detection_enabled", False))
        self._settings = settings
        self._connector = connector
        self._shutdown_seconds = float(shutdown_seconds)
        self._can_run = can_run or (lambda: True)
        self._on_update = on_update
        self._sessions: dict[str, _LiveSession] = {}
        self._tasks: set[asyncio.Task[LiveDetectionOutcome]] = set()
        self._pending_by_call: dict[str, set[asyncio.Task[LiveDetectionOutcome]]] = {}
        self.completed: deque[LiveDetectionOutcome] = deque(maxlen=self.MAX_HISTORY_CALLS)
        self._completed_by_call: OrderedDict[str, list[LiveDetectionOutcome]] = OrderedDict()
        self.decisions: dict[str, DetectionDecision] = {}
        self._stream_counts: dict[str, int] = {}
        self._budget_samples: dict[str, int] = {}
        self.closed = False

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def _publish(self, call_sid: str, decision: DetectionDecision | None = None,
                 *, reason: str | None = None) -> None:
        if self._on_update is None:
            return
        payload = {
            "provider": "modulate", "status": "analyzing", "label": "unknown",
            "confidence": None, "reason": reason or "analyzing",
            "streams": self._stream_counts.get(call_sid, 0),
            "observations": 0, "accepted_frames": 0, "dropped_frames": 0,
            "submitted_audio_ms": 0, "coverage_limited": False,
        }
        if decision:
            payload.update({key: value for key, value in decision.to_dict().items()
                            if key != "session_id"})
            payload["status"] = "complete" if decision.label != "unknown" else "unknown"
        elif reason:
            payload["status"] = "unknown"
        if reason:
            payload["reason"] = reason
        try:
            self._on_update(call_sid, payload)
        except Exception:
            # Persistence is separately supervised by the application, never by telephony.
            pass

    def start(self, call_sid: str, stream_sid: str) -> None:
        if not self.enabled or self.closed or not self._can_run():
            return
        remaining_samples = (self._settings.modulate_detection_max_audio_seconds * 8000
                             - self._budget_samples.get(call_sid, 0))
        if remaining_samples <= 0:
            return
        previous = self._sessions.get(call_sid)
        if previous is not None:
            if previous.stream_id == stream_sid and not previous.task.done():
                return
            previous.worker.finish("incomplete_audio")
        if self.active_count >= self.MAX_ACTIVE_STREAMS:
            self._publish(call_sid, reason="capacity_exceeded")
            return
        if self._stream_counts.get(call_sid, 0) >= self.MAX_STREAMS_PER_CALL:
            self._publish(call_sid, reason="too_many_streams")
            return
        worker = LiveDetectionWorker(
            api_key=self._settings.modulate_api_key,
            queue_frames=self._settings.modulate_detection_queue_frames,
            deadline_seconds=self._settings.modulate_detection_deadline_seconds,
            max_audio_seconds=math.ceil(remaining_samples / 8000),
            connector=self._connector,
        )
        self._stream_counts[call_sid] = self._stream_counts.get(call_sid, 0) + 1
        task = asyncio.create_task(worker.run(), name="live-modulate-detection")
        session = _LiveSession(stream_sid, worker, task)
        self._sessions[call_sid] = session
        self._tasks.add(task)
        self._pending_by_call.setdefault(call_sid, set()).add(task)
        self._publish(call_sid)
        task.add_done_callback(
            lambda finished, sid=call_sid, current=session: self._task_done(sid, current, finished))

    def offer(self, call_sid: str, track: str, timestamp_ms: int, payload: bytes) -> None:
        """Convert only bounded, validated inbound media; never wait for I/O."""
        if track != "inbound":
            return
        session = self._sessions.get(call_sid)
        if session is None or session.task.done():
            return
        remaining_samples = (self._settings.modulate_detection_max_audio_seconds * 8000
                             - self._budget_samples.get(call_sid, 0))
        if remaining_samples <= 0:
            session.worker.finish(coverage_limited=True)
            return
        try:
            frame = AudioFrame(call_sid, session.stream_id, "inbound", timestamp_ms,
                               decode_mulaw(payload[:remaining_samples]))
        except (TypeError, ValueError):
            session.worker.finish("incomplete_audio")
            return
        if session.worker.offer(frame):
            # Accepted input reserves the shared call budget, including older streams
            # still finalizing. Never refund failed or unsent reservations.
            used = self._budget_samples.get(call_sid, 0) + frame.sample_count
            self._budget_samples[call_sid] = used
            if used >= self._settings.modulate_detection_max_audio_seconds * 8000:
                session.worker.finish(coverage_limited=True)

    def finish(self, call_sid: str, reason: str = "call-ended") -> None:
        session = self._sessions.get(call_sid)
        if session is not None:
            session.worker.finish(None if reason in {"call-ended", "stream-stopped"}
                                  else "incomplete_audio")

    def _task_done(self, call_sid: str, session: _LiveSession,
                   task: asyncio.Task[LiveDetectionOutcome]) -> None:
        self._tasks.discard(task)
        pending = self._pending_by_call.get(call_sid, set())
        pending.discard(task)
        if not pending:
            self._pending_by_call.pop(call_sid, None)
        current = self._sessions.get(call_sid)
        if current is session:
            self._sessions.pop(call_sid, None)
        try:
            if task.cancelled():
                raise asyncio.CancelledError
            outcome = task.result()
        except BaseException as exc:
            # A cancelled/shutdown attempt must leave a durable unknown, not analyzing forever.
            outcome = LiveDetectionOutcome(
                DetectionReport(call_sid, session.stream_id,
                                reason="cancelled" if isinstance(exc, asyncio.CancelledError) else "internal_error"),
                session.worker._accepted_frames, session.worker.dropped_frames)
        self.completed.append(outcome)
        call_outcomes = self._completed_by_call.setdefault(call_sid, [])
        if len(call_outcomes) < self.MAX_STREAMS_PER_CALL:
            call_outcomes.append(outcome)
        self._completed_by_call.move_to_end(call_sid)
        decision = decide_call_detection(call_outcomes,
                        min_confidence=getattr(self._settings, "modulate_detection_min_confidence", 0.80))
        self.decisions[call_sid] = decision
        reason = outcome.report.reason if decision.reason == "provider_incomplete" else None
        # An older reconnect result cannot overwrite the current stream's analyzing state.
        if not pending:
            self._publish(call_sid, decision, reason=reason)
        while len(self._completed_by_call) > self.MAX_HISTORY_CALLS:
            expired = next((sid for sid in self._completed_by_call if sid not in self._pending_by_call), None)
            if expired is None:
                break
            self._completed_by_call.pop(expired)
            self.decisions.pop(expired, None)
            self._stream_counts.pop(expired, None)
            self._budget_samples.pop(expired, None)

    async def wait_idle(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
            await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed = True
        for session in self._sessions.values():
            session.worker.finish("incomplete_audio")
        tasks = list(self._tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=self._shutdown_seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)
