"""Adapt authenticated operator legs to the existing dashboard data pipeline.

One real remote CallSid and first remote StreamSid identify a call. The router
supplies session-relative timestamps across both legs/reconnects. Only the
remote microphone reaches caller detection; output is recorded separately.
"""

import asyncio
import logging

log = logging.getLogger("uvicorn.error")


class BridgePipeline:
    def __init__(self, settings, capture, transcription, detection, details):
        self.settings = settings
        self.capture = capture
        self.transcription = transcription
        self.detection = detection
        self.details = details
        self.controller = None
        self.calls = {}
        self.started = set()
        self.capture_failed = set()
        self.events = asyncio.Queue(maxsize=128)
        self.worker = None
        self.closed = False
        self.fallbacks = set()
        self.release_tasks = set()
        self.ending = set()

    @property
    def active_call_ids(self):
        return set(self.calls)

    @property
    def pending_count(self):
        return self.events.qsize() + len(self.fallbacks)

    async def start(self, session):
        sid = session.canonical_call_sid
        if not sid or sid in self.calls or self.closed:
            return
        self.calls[sid] = session
        await asyncio.to_thread(self.details.start, sid, session.to, session.created_at)

    def _ensure(self, session):
        sid = session.canonical_call_sid
        if not sid or sid not in self.calls or self.closed:
            return False
        if sid not in self.started:
            remote = session.legs.get("remote")
            stream_sid = remote.stream_sid if remote else ""
            if not stream_sid:
                return False
            self.transcription.start(sid, stream_sid)
            transcript = self.transcription.sessions.get(sid)
            if transcript is not None:
                transcript.started_at = session.created_at
            self.detection.start(sid, stream_sid)
            try:
                self.capture.start_external(sid, stream_sid, started_at=session.created_at)
            except (ValueError, RuntimeError, OSError):
                self.capture_failed.add(sid)
                log.warning("bridge_capture_unavailable")
            self.started.add(sid)
        for role, leg in session.legs.items():
            self.capture.note_source(sid, role=role, stream_sid=leg.stream_sid,
                                     generation=leg.generation)
        return True

    def audio(self, session, role, frame, timestamp_ms):
        if role != "remote" or not self._ensure(session):
            return
        sid = session.canonical_call_sid
        if sid not in self.capture_failed:
            self.capture.offer_external(sid, "inbound", timestamp_ms, frame)
        self.transcription.offer(sid, "inbound", timestamp_ms, frame)
        self.detection.offer(sid, "inbound", timestamp_ms, frame)

    def output(self, session, frame, timestamp_ms, kind):
        if not self._ensure(session):
            return
        sid = session.canonical_call_sid
        if sid not in self.capture_failed:
            self.capture.offer_external(sid, "outbound", timestamp_ms, frame)
        # Silence keeps Deepgram's clock aligned without classifying generated
        # output as the owner's microphone. Delivered agent text is added below.
        self.transcription.offer(sid, "outbound", timestamp_ms,
                                 frame if kind == "human" else b"\xff" * len(frame))

    async def agent_turn(self, session, text, start_ms, end_ms, **metadata):
        self.transcription.add_agent_segment(session.canonical_call_sid, text,
            int(start_ms), int(end_ms), name=metadata.get("agent_name", "Agent"),
            delivery=metadata.get("delivery", "played"))

    def context(self, session):
        live = self.transcription.sessions.get(session.canonical_call_sid)
        if (live is None or live.finishing or any(
                track.error or track.finishing for track in live.tracks.values())):
            raise RuntimeError("Transcription is unavailable for takeover")
        return [{"speaker": "agent" if row.get("source") == "agent" else
                 "remote" if row["track"] == "inbound" else "owner",
                 "text": row["text"], "id": row["id"],
                 "start_ms": row["start_ms"], "end_ms": row["end_ms"],
                 "delivery": row.get("delivery", "played")}
                for row in self.transcription.call_segments(session.canonical_call_sid)]

    def transcription_failed(self, call_sid, track):
        session = self.calls.get(call_sid)
        if session is not None and call_sid not in self.ending and not self.closed:
            self._schedule_release(session.id)

    def _schedule_release(self, session_id):
        if self.controller is None or session_id in self.fallbacks:
            return
        self.fallbacks.add(session_id)
        task = asyncio.create_task(self._release(session_id))
        self.release_tasks.add(task)
        task.add_done_callback(self.release_tasks.discard)

    def transcript_event(self, call_sid, segment, final):
        session = self.calls.get(call_sid)
        if session is None or self.closed or self.controller is None:
            return
        try:
            self.events.put_nowait((session.id, dict(segment), final))
        except asyncio.QueueFull:
            # A stale conversation cannot take over safely. Release once while
            # preserving the recorder/transcript independently of this listener.
            self._schedule_release(session.id)
            return
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._events(), name="bridge-transcript-events")

    async def _release(self, session_id):
        try:
            await self.controller.set_mode(session_id, "human")
        except Exception:
            log.warning("bridge_transcript_release_failed")
        finally:
            self.fallbacks.discard(session_id)

    async def _events(self):
        while not self.closed:
            try:
                session_id, segment, final = self.events.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self.controller.transcript(session_id,
                    "remote" if segment["track"] == "inbound" else "owner",
                    segment["text"], final=final, segment_id=segment.get("id", ""),
                    timestamp_ms=segment.get("start_ms"))
            except Exception:
                log.warning("bridge_transcript_listener_failed")
                await self._release(session_id)
            finally:
                self.events.task_done()

    async def end(self, session):
        sid = session.canonical_call_sid
        if not sid or sid not in self.calls or sid in self.ending:
            return
        self.ending.add(sid)
        try:
            self.transcription.finish(sid, "call-ended")
            self.detection.finish(sid, "call-ended")
            try:
                await self.capture.finish(sid)
            finally:
                await asyncio.to_thread(self.details.finish, sid)
        finally:
            self.calls.pop(sid, None)
            self.started.discard(sid)
            self.capture_failed.discard(sid)
            self.ending.discard(sid)

    async def close(self):
        for session in list(self.calls.values()):
            await self.end(session)
        self.closed = True
        if self.worker is not None:
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.release_tasks:
            await asyncio.gather(*list(self.release_tasks), return_exceptions=True)
