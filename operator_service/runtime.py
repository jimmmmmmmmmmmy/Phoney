"""Manual, cancellable Gemini/ElevenLabs dialogue over the existing audio router.

Provider work runs outside media readers. A bounded phrase queue connects Gemini
and streamed synthesis; frames are paced by CallRouter with real backpressure.
Only acknowledged phrases are recorded as fully delivered.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import time

import httpx

from voice_stack.agent import Conversation, ReplyCommandBuffer, SentenceBuffer, reply_events
from voice_stack.audio import FRAME_BYTES, iter_frames
from voice_stack.tts import speech, speech_bytes
from .sessions import AGENT, ANNOUNCING, HUMAN, PREPARING, REMOTE, OperatorRejected

PHRASE_QUEUE_SIZE = 4
MAX_CONTEXT_CHARS = 48_000
MAX_REPLY_CHARS = 8_000
MAX_AUDIO_BYTES = 8000 * 60
PREPARATION_SECONDS = 15.0
PLAYBACK_ACK_SECONDS = 5.0
FRAME_STALL_SECONDS = 2.0
ANNOUNCEMENT = "An AI assistant is joining this call."


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def handoff_context(turns):
    """Bound context without putting transcript text in trusted instructions."""
    selected, size = [], 0
    for turn in reversed(list(turns or [])[-200:]):
        if not isinstance(turn, dict):
            continue
        speaker = turn.get("speaker", turn.get("role", ""))
        speaker = {"caller": "remote", "inbound": "remote", "outbound": "owner"}.get(speaker, speaker)
        text = str(turn.get("text", "")).strip()
        if speaker not in ("owner", "remote", "agent") or not text:
            continue
        if turn.get("delivery") == "interrupted":
            text = "[Delivery interrupted; not all of this was heard] " + text
        text = text[:8000]
        if size + len(text) > MAX_CONTEXT_CHARS:
            break
        size += len(text)
        selected.append((speaker, text))
    return list(reversed(selected))


class DialogueRun:
    """One generation, canceled on #0, profile change, hangup, or barge-in."""
    def __init__(self, controller, session, snapshot, epoch, *, announce=False):
        self.controller, self.session, self.snapshot = controller, session, snapshot
        self.epoch, self.announce = epoch, announce
        self.voice = controller.voice
        self.phrases = asyncio.Queue(maxsize=PHRASE_QUEUE_SIZE)
        self.pending = None
        self.confirmed = []
        self.producer = None
        self.sequence = 0
        self.context_revision = controller._context_revisions.get(session.id, 0)
        self.takeover_request = controller._takeover_requests.get(session.id, 0)
        self.open_audio = None
        self.end_requested = False

    def current(self):
        return (self.session.active and self.session.reply_epoch == self.epoch
                and self.session.mode in (PREPARING, ANNOUNCING, AGENT))

    async def run(self):
        reply_exhausted = False
        try:
            async with asyncio.timeout(PREPARATION_SECONDS):
                turns = (await maybe_await(self.controller.context_getter(self.session))
                         if self.controller.context_getter else self.session.turns)
            conversation = Conversation.handoff(handoff_context(turns),
                goal=self.session.goal, boundaries=self.snapshot.prompt)
            if not conversation.contents:
                conversation.contents.append({"role": "user", "parts": [{"text":
                    "The owner selected you for this call. Begin using the selected instructions; "
                    "ask a short clarifying question if the goal is not specified."}]})
            async with httpx.AsyncClient(transport=self.controller.provider_transport) as http:
                self.producer = asyncio.create_task(self._produce(http, conversation))
                if self.announce:
                    async with asyncio.timeout(PREPARATION_SECONDS):
                        cue = await speech(http, self.voice.elevenlabs_api_key,
                            self.snapshot.voice_id, ANNOUNCEMENT,
                            model=self.voice.elevenlabs_model, output_format="ulaw_8000",
                            timeout=min(self.voice.request_timeout, PREPARATION_SECONDS))
                        if not cue or len(cue) > MAX_AUDIO_BYTES:
                            raise ValueError("Invalid announcement audio")
                        first = await self._next_phrase()
                        reply_exhausted = first is None
                        if first is None and not self.end_requested:
                            raise ValueError("Empty agent reply")
                        audio = None
                        if first is not None:
                            audio = self._speech(http, first)
                            self.open_audio = audio
                            # The first usable response must exist before muting the owner.
                            first_chunk = await anext(audio)
                            if not first_chunk:
                                raise ValueError("Empty agent audio")
                    if not self.current():
                        return
                    await self.controller.store.transition(self.session.id, self.epoch, ANNOUNCING)
                    self.controller.router(self.session.id).set_mode(ANNOUNCING)
                    await self._play_chunks(_bytes(cue), kind="announcement")
                    await self._ack("announcement")
                    if not self.current():
                        return
                    await self.controller.store.transition(self.session.id, self.epoch, AGENT)
                    self.controller.router(self.session.id).set_mode(AGENT)
                    if self.context_revision != self.controller._context_revisions.get(self.session.id, 0):
                        # Speech during the announcement is new evidence too;
                        # preserve the cue but replace the now-stale first reply.
                        await self.controller.store.invalidate_reply(self.session.id)
                        self.controller._start_dialogue(self.session)
                        return
                    if first is not None:
                        await self._phrase(first, _prepend(first_chunk, audio))
                while self.current() and not reply_exhausted:
                    phrase = await self._next_phrase()
                    if phrase is None:
                        break
                    await self._phrase(phrase, self._speech(http, phrase))
                await self.producer
                # The producer's successful completion and every phrase's
                # playback mark must precede hangup. A new owner selection
                # revokes this action even while its registry lookup is pending.
                if (self.end_requested and self.current()
                        and self.takeover_request == self.controller._takeover_requests.get(self.session.id, 0)):
                    await self.controller.end(self.session.id, "agent-end-call")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.current():
                await self.controller._abandon(self.session.id, "dialogue-" + type(exc).__name__)
        finally:
            if self.producer is not None and not self.producer.done():
                self.producer.cancel()
            if self.producer is not None:
                await asyncio.gather(self.producer, return_exceptions=True)
            if self.open_audio is not None:
                await self.open_audio.aclose()
            if (self.pending is not None and self.controller._agent_frames_sent.get(self.session.id, 0)
                    > self.pending["sent_before"]):
                await self._record(self.pending, "interrupted")
                self.pending = None

    async def _produce(self, http, conversation):
        buffer = SentenceBuffer()
        commands = ReplyCommandBuffer()
        chars = 0
        async for event in reply_events(http, self.voice.gemini_api_key,
                conversation.system, deepcopy(conversation.contents),
                model=self.voice.gemini_model, max_output_tokens=self.voice.max_reply_tokens,
                timeout=self.voice.request_timeout):
            if event["kind"] == "text":
                chars += len(event["text"])
                if chars > MAX_REPLY_CHARS:
                    raise ValueError("Reply too long")
                for phrase in buffer.feed(commands.feed(event["text"])):
                    await self.phrases.put(phrase)
        commands.finish()
        self.end_requested = commands.end_call
        trailing = buffer.flush()
        if trailing:
            await self.phrases.put(trailing)
        await self.phrases.put(None)

    async def _next_phrase(self):
        # A producer exception must wake a consumer waiting on an empty queue.
        get = asyncio.create_task(self.phrases.get())
        try:
            done, _ = await asyncio.wait((get, self.producer), return_when=asyncio.FIRST_COMPLETED)
            if get in done:
                return get.result()
            await self.producer
            return await get
        finally:
            if not get.done():
                get.cancel()
            await asyncio.gather(get, return_exceptions=True)

    def _speech(self, http, phrase):
        return speech_bytes(http, self.voice.elevenlabs_api_key, self.snapshot.voice_id,
            phrase, model=self.voice.elevenlabs_model, output_format="ulaw_8000",
            timeout=self.voice.request_timeout)

    async def _phrase(self, phrase, chunks):
        self.pending = {"text": phrase, "start": self.controller.elapsed_ms(self.session.id),
                        "frames": 0, "sent_before": self.controller._agent_frames_sent.get(self.session.id, 0)}
        await self._play_chunks(chunks, kind="agent")
        await self._ack("reply")
        if self.current():
            await self._record(self.pending, "played")
            self.confirmed.append(phrase)
            self.pending = None

    async def _play_chunks(self, chunks, *, kind):
        # A stream that keeps returning tiny chunks must not evade the timeout.
        async with asyncio.timeout(self.voice.request_timeout):
            await self._play_chunks_bounded(chunks, kind=kind)

    async def _play_chunks_bounded(self, chunks, *, kind):
        buffer = bytearray()
        size = 0
        try:
            async for chunk in chunks:
                if not self.current():
                    raise asyncio.CancelledError
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise ValueError("Phrase audio too long")
                buffer.extend(chunk)
                while len(buffer) >= FRAME_BYTES:
                    frame = bytes(buffer[:FRAME_BYTES])
                    del buffer[:FRAME_BYTES]
                    await self._frame(frame, kind)
            if buffer:
                await self._frame(next(iter_frames(bytes(buffer))), kind)
            if size == 0:
                raise ValueError("No speech audio")
        finally:
            close = getattr(chunks, "aclose", None)
            if close:
                await close()

    async def _frame(self, frame, kind):
        router = self.controller.router(self.session.id)
        deadline = time.monotonic() + FRAME_STALL_SECONDS
        while router.pending_agent(REMOTE) >= 4:
            if not self.current():
                raise asyncio.CancelledError
            if time.monotonic() >= deadline:
                raise TimeoutError("Audio writer stalled")
            await asyncio.sleep(0.01)
        if not self.current():
            raise asyncio.CancelledError
        sent = router.send_announcement(frame) if kind == "announcement" else router.send_agent((frame,))
        if not sent:
            raise ValueError("Remote stream unavailable")
        if kind == "agent" and self.pending is not None:
            self.pending["frames"] += 1

    async def _ack(self, kind):
        self.sequence += 1
        name = f"{kind}-{self.epoch}-{self.sequence}"
        await self.controller.wait_for_mark(self.session.id, name,
            timeout=PLAYBACK_ACK_SECONDS + 1.0)
        if not self.current():
            raise asyncio.CancelledError

    async def _record(self, pending, delivery):
        end = self.controller.elapsed_ms(self.session.id)
        if delivery == "played":
            await self.controller.store.add_turn(self.session.id, "agent", pending["text"])
        await self.controller.agent_turn(self.session, pending["text"], pending["start"], end,
            agent_name=self.snapshot.name, delivery=delivery, agent_id=self.snapshot.id)


async def _bytes(audio):
    yield audio


async def _prepend(first, source):
    try:
        yield first
        async for chunk in source:
            yield chunk
    finally:
        await source.aclose()
