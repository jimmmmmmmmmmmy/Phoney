"""Non-blocking bounded input queue for live Modulate shadow detection."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
import time
from typing import AsyncIterator, Callable

from integrations.contracts import AudioFrame
from media_capture.capture import decode_mulaw
from .modulate import (Connector, DEFAULT_DEADLINE_SECONDS, DetectionReport,
                       MAX_AUDIO_SECONDS, stream_inbound_pcm)
from .policy import DetectionDecision, decide_call_detection
from .analysis import MAX_WINDOWS, build_analysis


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
                 connector: Connector | None = None, on_observation=None) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if type(queue_frames) is not int or queue_frames < 1:
            raise ValueError("queue_frames must be a positive integer")
        self._api_key = api_key
        self._queue: asyncio.Queue[AudioFrame | object] = asyncio.Queue(maxsize=queue_frames)
        self._deadline_seconds = deadline_seconds
        self._max_audio_seconds = max_audio_seconds
        self._connector = connector
        self._on_observation = on_observation
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
                on_observation=self._on_observation,
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
    samples: int = 0
    windows: list[dict] = field(default_factory=list)
    rotated: bool = False


class LiveDetectionManager:
    """Keep provider work isolated from calls, with bounded advisory history."""

    MAX_ACTIVE_STREAMS = 32
    MAX_HISTORY_CALLS = 128
    MAX_STREAMS_PER_CALL = 8  # Twilio reconnect epochs, not provider windows.
    MAX_PENDING_PER_CALL = 2  # One collecting socket and at most one finalizing.
    RECOVERY_SECONDS = (5, 15, 30)

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
        self._epoch_counts: dict[str, int] = {}
        self._sources: dict[str, str] = {}
        self._retry_after: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._limited: set[str] = set()
        self._budget_samples: dict[str, int] = {}
        self._windows: dict[str, list[dict]] = {}
        self._last_publication: dict[str, float] = {}
        self.closed = False

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    @property
    def active_call_ids(self) -> set[str]:
        return set(self._pending_by_call) | set(self._sources)

    def _observe(self, call_sid, session, observation):
        windows = self._windows.setdefault(call_sid, [])
        if len(windows) >= MAX_WINDOWS:
            del windows[0]
            self._limited.add(call_sid)
        window = {"stream_id": observation.stream_id, "start_ms": observation.start_ms,
                  "end_ms": observation.end_ms, "verdict": observation.provider_verdict,
                  "confidence": observation.confidence}
        windows.append(window)
        session.windows.append(window)
        if time.monotonic() - self._last_publication.get(call_sid, 0) >= 2:
            self._publish(call_sid)

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
        outcomes = self._completed_by_call.get(call_sid, ())
        complete = bool(decision and not self._pending_by_call.get(call_sid)
                        and call_sid not in self._sources and call_sid not in self._limited
                        and not decision.coverage_limited and not decision.dropped_frames
                        and all(item.report.reason in {None, "no_usable_content"} for item in outcomes))
        payload["analysis"] = build_analysis(self._windows.get(call_sid, []),
                          min_confidence=getattr(self._settings, "modulate_detection_min_confidence", .8),
                          source="live", complete=complete)
        payload["coverage_limited"] = payload["coverage_limited"] or call_sid in self._limited
        if not decision:
            payload["observations"] = len(self._windows.get(call_sid, []))
        self._last_publication[call_sid] = time.monotonic()
        try:
            self._on_update(call_sid, payload)
        except Exception:
            # Persistence is separately supervised by the application, never by telephony.
            pass

    @property
    def _call_sample_limit(self):
        return min(getattr(self._settings, "max_call_seconds", 1800),
                   getattr(self._settings, "media_max_seconds", 3600), 3600) * 8000

    def start(self, call_sid: str, stream_sid: str) -> None:
        if not self.enabled or self.closed or not self._can_run():
            return
        if self._sources.get(call_sid) == stream_sid:
            return
        previous = self._sessions.get(call_sid)
        if previous is not None:
            previous.worker.finish("incomplete_audio")
        if self._epoch_counts.get(call_sid, 0) >= self.MAX_STREAMS_PER_CALL:
            # Revoke the previous epoch before rejecting the new one. Incoming
            # frames carry only call_sid, so keeping the old source here could
            # reopen a provider worker and attribute new audio to the old clock.
            self._sources.pop(call_sid, None)
            self._limited.add(call_sid)
            self._publish(call_sid, reason="too_many_streams")
            return
        self._epoch_counts[call_sid] = self._epoch_counts.get(call_sid, 0) + 1
        self._sources[call_sid] = stream_sid
        self._retry_after.pop(call_sid, None)
        self._new_session(call_sid)

    def _new_session(self, call_sid):
        if (self.closed or not self._can_run() or call_sid not in self._sources
                or self._budget_samples.get(call_sid, 0) >= self._call_sample_limit
                or time.monotonic() < self._retry_after.get(call_sid, 0)):
            return None
        if (self.active_count >= self.MAX_ACTIVE_STREAMS
                or len(self._pending_by_call.get(call_sid, ())) >= self.MAX_PENDING_PER_CALL):
            self._limited.add(call_sid)
            if time.monotonic() - self._last_publication.get(call_sid, 0) >= 2:
                self._publish(call_sid, reason="capacity_exceeded")
            return None
        worker = LiveDetectionWorker(
            api_key=self._settings.modulate_api_key,
            queue_frames=self._settings.modulate_detection_queue_frames,
            deadline_seconds=self._settings.modulate_detection_deadline_seconds,
            max_audio_seconds=self._settings.modulate_detection_max_audio_seconds,
            connector=self._connector,
            on_observation=lambda observation: self._observe(call_sid, session, observation),
        )
        self._stream_counts[call_sid] = self._stream_counts.get(call_sid, 0) + 1
        task = asyncio.create_task(worker.run(), name="live-modulate-detection")
        session = _LiveSession(self._sources[call_sid], worker, task)
        self._sessions[call_sid] = session
        self._tasks.add(task)
        self._pending_by_call.setdefault(call_sid, set()).add(task)
        self._publish(call_sid)
        task.add_done_callback(
            lambda finished, sid=call_sid, current=session: self._task_done(sid, current, finished))
        return session

    def offer(self, call_sid: str, track: str, timestamp_ms: int, payload: bytes) -> None:
        """Rotate bounded windows, submitting only new caller audio without waiting.

        Original stream IDs and timestamps survive rotation. A slow/failing
        provider loses bounded audio, then recovers on fresh input; it never
        accumulates an unbounded backlog or resubmits paid live audio.
        """
        if track != "inbound" or call_sid not in self._sources or self.closed:
            return
        if type(timestamp_ms) is not int or timestamp_ms < 0 or not isinstance(payload, bytes):
            session = self._sessions.get(call_sid)
            if session:
                session.worker.finish("incomplete_audio")
            return
        offset = 0
        while offset < len(payload):
            remaining = self._call_sample_limit - self._budget_samples.get(call_sid, 0)
            if remaining <= 0:
                self._limited.add(call_sid)
                return
            session = self._sessions.get(call_sid)
            if session is None or session.task.done() or session.worker._finished:
                session = self._new_session(call_sid)
                if session is None:
                    self._limited.add(call_sid)
                    return
            window_left = self._settings.modulate_detection_max_audio_seconds * 8000 - session.samples
            size = min(len(payload) - offset, window_left, remaining)
            try:
                frame = AudioFrame(call_sid, session.stream_id, "inbound", timestamp_ms + offset // 8,
                                   decode_mulaw(payload[offset:offset + size]))
            except (TypeError, ValueError):
                session.worker.finish("incomplete_audio")
                return
            if not session.worker.offer(frame):
                self._limited.add(call_sid)
                session.worker.finish("incomplete_audio")
                return
            session.samples += frame.sample_count
            self._budget_samples[call_sid] = self._budget_samples.get(call_sid, 0) + frame.sample_count
            offset += size
            if session.samples == self._settings.modulate_detection_max_audio_seconds * 8000:
                session.rotated = True
                session.worker.finish()
            elif self._budget_samples[call_sid] >= self._call_sample_limit:
                session.worker.finish(coverage_limited=True)

    def finish(self, call_sid: str, reason: str = "call-ended") -> None:
        self._sources.pop(call_sid, None)
        session = self._sessions.get(call_sid)
        if session is not None:
            session.worker.finish(None if reason in {"call-ended", "stream-stopped"}
                                  else "incomplete_audio")
        elif not self._pending_by_call.get(call_sid) and call_sid in self.decisions:
            self._publish(call_sid, self.decisions[call_sid])

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
        if session.rotated and outcome.report.reason in {None, "no_usable_content"}:
            outcome = replace(outcome, report=replace(outcome.report, coverage_limited=False))
        if outcome.report.reason not in {None, "no_usable_content"}:
            failures = self._failures.get(call_sid, 0) + 1
            self._failures[call_sid] = failures
            self._retry_after[call_sid] = time.monotonic() + self.RECOVERY_SECONDS[min(failures - 1, 2)]
        else:
            self._failures.pop(call_sid, None)
            self._retry_after.pop(call_sid, None)
        invalid_evidence = outcome.report.reason in {"invalid_provider_response", "audio_discontinuity",
                            "mixed_sessions", "incomplete_audio", "internal_error"}
        if invalid_evidence:
            self._windows[call_sid] = [window for window in self._windows.get(call_sid, [])
                                      if not any(window is item for item in session.windows)]
        elif outcome.report.reason == "no_usable_content":
            self._windows[call_sid] = [window for window in self._windows.get(call_sid, [])
                                      if not any(window is item for item in session.windows) or window["verdict"] == "no-content"]
        self.completed.append(outcome)
        call_outcomes = self._completed_by_call.setdefault(call_sid, [])
        if len(call_outcomes) >= MAX_WINDOWS:
            del call_outcomes[0]
            self._limited.add(call_sid)
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
            expired = next((sid for sid in self._completed_by_call if sid not in self.active_call_ids), None)
            if expired is None:
                break
            self._completed_by_call.pop(expired)
            self.decisions.pop(expired, None)
            self._stream_counts.pop(expired, None)
            self._epoch_counts.pop(expired, None)
            self._retry_after.pop(expired, None)
            self._failures.pop(expired, None)
            self._limited.discard(expired)
            self._budget_samples.pop(expired, None)
            self._windows.pop(expired, None)
            self._last_publication.pop(expired, None)

    async def wait_idle(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
            await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed = True
        self._sources.clear()
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
