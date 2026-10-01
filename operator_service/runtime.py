"""Cancellable phone-agent dialogue over the existing audio router.

Provider work runs outside media readers. A bounded phrase queue connects Gemini
and streamed synthesis; frames are paced by CallRouter with real backpressure.
Only acknowledged phrases are recorded as fully delivered.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import re
import time

import httpx

from voice_stack.agent import (Conversation, GeminiError, ReplyCommandBuffer, SentenceBuffer,
                               reply_events_with_retry as reply_events)
from voice_stack.audio import FRAME_BYTES, iter_frames
from voice_stack.tts import SpeechSession, TTSError, speech
from voice_stack.prompts import (VOICEMAIL_GREETING,
                                 three_reply_phase_instruction, voicemail_phase_instruction)
from voice_stack.voicemail_recovery import recovery_reply
from .sessions import AGENT, ANNOUNCING, HUMAN, OWNER, PREPARING, REMOTE, OperatorRejected
from .audio import AGENT_QUEUE_FRAMES

PHRASE_QUEUE_SIZE = 4
MAX_CONTEXT_CHARS = 48_000
MAX_REPLY_CHARS = 8_000
MAX_AUDIO_BYTES = 8000 * 60
PREFETCH_AUDIO_BYTES = 8000
PREPARATION_SECONDS = 15.0
FIRST_TEXT_SECONDS = 3.0
VOICEMAIL_FIRST_TEXT_SECONDS = 4.0
VOICEMAIL_PHRASE_CHARS = 240
PLAYBACK_ACK_SECONDS = 5.0
FRAME_STALL_SECONDS = 2.0
COALESCE_SECONDS = 0.2
COALESCE_CHARS = 160
ANNOUNCEMENT = "An AI assistant is joining this call."
OWNER_NOTICE = "AI Detected, deploying voice agent"


async def cached_static_audio(controller, snapshot, text):
    """Share bounded synthesis across ringing and playback; never synthesize twice.

    The greeting has no caller-specific data. It is cached by the selected voice,
    model, and exact text. A canceled waiter cannot cancel another call's work;
    when the last waiter leaves, unfinished synthesis is canceled as well.
    """
    voice = controller.voice
    key = (snapshot.voice_id, voice.elevenlabs_model, voice.delivery.cache_key, text)
    cache = controller.announcement_cache
    if key in cache:
        return cache[key]
    tasks, waiters = controller._voicemail_greeting_tasks, controller._voicemail_greeting_waiters
    task = tasks.get(key)
    if task is None:
        async def prepare():
            async with asyncio.timeout(PREPARATION_SECONDS):
                async with httpx.AsyncClient(transport=controller.provider_transport) as http:
                    audio = await speech(http, voice.elevenlabs_api_key, snapshot.voice_id,
                        text, model=voice.elevenlabs_model, output_format="ulaw_8000",
                        delivery=voice.delivery,
                        timeout=min(voice.request_timeout, PREPARATION_SECONDS))
            if not audio or len(audio) > MAX_AUDIO_BYTES:
                raise ValueError("Invalid voicemail greeting audio")
            if len(cache) >= 16:
                cache.pop(next(iter(cache)))
            cache[key] = audio
            return audio
        task = controller.store.spawn(prepare())
        tasks[key] = task
    waiters[key] = waiters.get(key, 0) + 1
    try:
        return await asyncio.shield(task)
    finally:
        remaining = waiters[key] - 1
        if remaining:
            waiters[key] = remaining
        else:
            waiters.pop(key, None)
            if tasks.get(key) is task:
                tasks.pop(key, None)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def voicemail_greeting_audio(controller, snapshot):
    return await cached_static_audio(controller, snapshot, VOICEMAIL_GREETING)


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
        self.remote_revision = controller._remote_revisions.get(session.id, 0)
        self.speaking_started_ms = None
        self.completed = False
        self.reply_number = controller._agent_reply_counts.get(session.id, 0) + 1
        self.takeover_request = controller._accepted_takeovers.get(session.id, 0)
        self.open_audio = None
        self.synthesis = None
        self.prefetch_task = None
        self.prefetch_audio = None
        self.end_requested = False
        self.end_deferred = False
        self.cue_task = None
        self.preparation_task = None
        self.announcement_task = None
        self.owner_cue_task = None
        self.native_preparation_task = None
        self.held_phrase = None
        self.phrases_ended = False
        self.synthesis_sequence = 0
        self.voicemail_phase = getattr(session, "voicemail_phase", "greeting")
        self.voicemail = getattr(session, "voicemail", False)
        self.bounded_replies = getattr(session, "agent_kind", "manual") == "ai-detected"
        self.caller_requested_end = False
        self.context = []
        self.stage = "context"
        self.started = time.monotonic()

    def trace(self, event, **fields):
        self.controller.trace(self.session.id, event, run_epoch=self.epoch,
            run_ms=int((time.monotonic() - self.started) * 1000), **fields)

    def current(self):
        return (self.session.active and self.session.reply_epoch == self.epoch
                and self.session.mode in (PREPARING, ANNOUNCING, AGENT))

    async def run(self):
        self.trace("generation-started", announce=self.announce, agent_id=self.snapshot.id,
                   agent_revision=self.snapshot.revision, agent_reply_number=self.reply_number,
                   model=self.voice.gemini_model)
        try:
            async with asyncio.timeout(PREPARATION_SECONDS):
                turns = (await maybe_await(self.controller.context_getter(self.session))
                         if self.controller.context_getter else self.session.turns)
            context = handoff_context(turns)
            self.context = context
            latest_caller = next((text for speaker, text in reversed(context) if speaker == "remote"), "")
            self.caller_requested_end = bool(re.search(
                r"^(?:(?:ok(?:ay)?|thanks?|thank you)[,.! ]+)*(?:goodbye|bye(?: bye)?|hang up|"
                r"please (?:hang up|end (?:the|this) call)|end (?:the|this) call)[.! ]*$",
                re.split(r"(?<=[.!?])\s+", latest_caller.strip())[-1], re.I))
            instruction = (voicemail_phase_instruction(self.voicemail_phase) if self.voicemail
                           else three_reply_phase_instruction(self.reply_number) if self.bounded_replies else "")
            conversation = Conversation.handoff(context,
                goal=self.session.goal, boundaries=self.snapshot.prompt,
                agent_reply_number=self.reply_number, announcement_provided=not self.voicemail,
                runtime_instruction=instruction)
            if not conversation.contents:
                conversation.contents.append({"role": "user", "parts": [{"text":
                    "The phone runtime activated you for this call. Begin using the selected instructions; "
                    "ask a short clarifying question if the goal is not specified."}]})
            async with self.controller.provider_client(self.session.id) as http:
                self.producer = asyncio.create_task(self._produce(http, conversation))
                await self._deliver_phrases(http)
                await self.producer
                self.completed = True
                if (self.confirmed and self.current()
                        and self.takeover_request == self.controller._takeover_requests.get(self.session.id, 0)):
                    self.controller._agent_reply_counts[self.session.id] = self.reply_number
                    self.trace("agent-reply-completed", agent_reply_number=self.reply_number)
                if self.bounded_replies:
                    if self.reply_number >= 3 and self.confirmed:
                        self.end_requested = True
                    elif not self.caller_requested_end:
                        self.end_requested = False
                if self.voicemail and self.voicemail_phase in ("greeting", "capture", "readback"):
                    self.end_requested = self.end_requested and self.caller_requested_end
                # The producer's successful completion and every phrase's
                # playback mark must precede hangup. A new owner selection
                # revokes this action even while its registry lookup is pending.
                if (self.end_requested and self.current()
                        and self.takeover_request == self.controller._takeover_requests.get(self.session.id, 0)):
                    if self.remote_revision != self.controller._remote_revisions.get(self.session.id, 0):
                        # A final caller turn received after the context snapshot
                        # must be considered before executing a stale hangup.
                        self.end_requested = False
                        self.end_deferred = True
                        self.trace("end-call-deferred-for-new-speech")
                    else:
                        self.trace("agent-end-call")
                        await self.controller.end(self.session.id, "agent-end-call")
                if self.voicemail and self.current() and (self.confirmed or self.end_deferred):
                    # Terminal voicemail phases also hang up in reply_completed,
                    # even when Gemini omits its command. Fresh caller activity
                    # must revoke that second ending path as well. Keep pending
                    # finalized speech so the ordinary quiet-period policy can
                    # read back or confirm it instead of ending the call.
                    terminal_phase = self.voicemail_phase in {"no_message", "unconfirmed", "complete", "followup_timeout"}
                    fresh_caller = self.remote_revision != self.controller._remote_revisions.get(self.session.id, 0)
                    if self.end_deferred or (terminal_phase and fresh_caller):
                        self.end_deferred = True
                        self.trace("voicemail-resume-after-deferred-end", phase=self.voicemail_phase)
                    else:
                        await self.controller.voicemail_reply_completed(self.session.id, self.voicemail_phase)
        except asyncio.CancelledError:
            self.trace("generation-canceled", stage=self.stage)
            raise
        except Exception as exc:
            fields = {}
            if isinstance(exc, httpx.HTTPStatusError):
                fields["http_status"] = exc.response.status_code
                fields["provider"] = {
                    "generativelanguage.googleapis.com": "gemini",
                    "api.elevenlabs.io": "elevenlabs",
                }.get(exc.request.url.host, "unknown")
            elif isinstance(exc, TTSError):
                fields["provider"] = "elevenlabs"
                if exc.http_status is not None:
                    fields["http_status"] = exc.http_status
            self.trace("generation-failed", stage=self.stage, error=type(exc).__name__, **fields)
            if self.current():
                await self.controller._abandon(self.session.id, "dialogue-" + type(exc).__name__)
        finally:
            tasks = [t for t in (self.cue_task, self.owner_cue_task, self.preparation_task, self.announcement_task) if t is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.producer is not None and not self.producer.done():
                self.producer.cancel()
            if self.producer is not None:
                await asyncio.gather(self.producer, return_exceptions=True)
            await self._close_prepared_audio()
            if (self.pending is not None and self.controller._agent_frames_sent.get(self.session.id, 0)
                    > self.pending["sent_before"]):
                await self._record(self.pending, "interrupted")
                self.pending = None
            if (self.completed and self.voicemail and self.end_deferred and self.current()
                    and self.takeover_request == self.controller._takeover_requests.get(self.session.id, 0)):
                # Arm the next quiet period only after every awaited cleanup.
                # Even a short pause cannot race this run's provider teardown.
                voicemail = self.controller._voicemail_agents.get(self.session.id)
                if voicemail is not None:
                    voicemail.resume_listening()

    async def _deliver_phrases(self, http):
        """Prepare one next phrase while the current one plays, never ahead of it."""
        async with SpeechSession(http, self.voice.elevenlabs_api_key, self.snapshot.voice_id,
                model=self.voice.elevenlabs_model, output_format="ulaw_8000",
                timeout=self.voice.request_timeout, delivery=self.voice.delivery) as synthesis:
            self.synthesis = synthesis
            try:
                if self.announce:
                    async with asyncio.timeout(PREPARATION_SECONDS):
                        if not self.voicemail:
                            self.cue_task = asyncio.create_task(self._announcement(http))
                        prepare_mode = getattr(self.controller, "prepare_audio_mode", None)
                        if getattr(self.session, "native_conference", False) and prepare_mode:
                            self.native_preparation_task = asyncio.create_task(
                                prepare_mode(self.session.id, self.epoch))
                        if (getattr(self.session, "agent_kind", "manual") == "ai-detected"
                                and not getattr(self.session, "native_conference", False)):
                            self.owner_cue_task = asyncio.create_task(self._announcement(http, OWNER_NOTICE))
                        self.preparation_task = asyncio.create_task(self._prepare_first(http))
                        self.announcement_task = asyncio.create_task(self._announce_ready())
                        prepared, _ = await asyncio.gather(self.preparation_task, self.announcement_task)
                    if not self.current():
                        return
                    await self.controller.transition_audio_mode(self.session.id, self.epoch, AGENT)
                    self.trace("agent-active")
                else:
                    prepared = await self._prepare_first(http)
                while self.current() and prepared[0] is not None:
                    phrase, audio, first_chunk = prepared
                    self.open_audio = audio
                    # Only this one next iterator may read ahead. It pauses at
                    # its first bounded chunk until the current playback mark.
                    previous_text = " ".join([*self.confirmed, phrase])[-1000:]
                    self.prefetch_task = asyncio.create_task(self._prefetch_next(http, previous_text))
                    await self._phrase(phrase, _prepend(first_chunk, audio))
                    self.open_audio = None
                    prepared = await self.prefetch_task
                    self.prefetch_task = None
                    # Keep the prepared iterator owned even if this generation
                    # is revoked before the next loop condition is evaluated.
                    self.open_audio = prepared[1]
                    self.prefetch_audio = None
            finally:
                # Stop readers before the adapter closes their HTTP/WS streams.
                await self._close_prepared_audio()
                self.synthesis = None

    async def _prefetch_next(self, http, previous_text):
        phrase = await self._next_phrase()
        if phrase is None:
            return None, None, None
        if not self.current():
            raise asyncio.CancelledError
        audio = self._speech(http, phrase, previous_text=previous_text)
        self.prefetch_audio = audio
        first_chunk = await anext(audio)
        if not first_chunk or len(first_chunk) > PREFETCH_AUDIO_BYTES:
            raise ValueError("Invalid prefetched audio chunk")
        return phrase, audio, first_chunk

    async def _close_prepared_audio(self):
        tasks = [task for task in (self.prefetch_task, self.preparation_task,
            self.announcement_task, self.cue_task, self.owner_cue_task) if task is not None]
        if self.native_preparation_task is not None:
            tasks.append(self.native_preparation_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.prefetch_task = None
        for audio in dict.fromkeys((self.prefetch_audio, self.open_audio)):
            if audio is not None:
                await audio.aclose()
        self.prefetch_audio = self.open_audio = None

    async def _prepare_first(self, http):
        self.stage = "first-phrase"
        first = await self._next_phrase()
        if first is None:
            if not (self.end_requested or self.end_deferred):
                raise ValueError("Empty agent reply")
            return None, None, None
        self.stage = "first-audio"
        if self.voicemail and self.voicemail_phase == "greeting":
            greeting = await voicemail_greeting_audio(self.controller, self.snapshot)
            self._source_audio("voicemail-greeting", greeting)
            audio = _bytes(greeting)
        else:
            audio = self._speech(http, first)
        self.open_audio = audio
        chunk = await anext(audio)
        if not chunk:
            raise ValueError("Empty agent audio")
        return first, audio, chunk

    async def _announce_ready(self):
        if self.voicemail:
            return
        cue = await self.cue_task
        # Avoid a misleading cue if reply preparation has already failed.
        if self.preparation_task.done():
            self.preparation_task.result()
        if not self.current():
            return
        if self.native_preparation_task is not None:
            await self.native_preparation_task
        await self.controller.transition_audio_mode(self.session.id, self.epoch, ANNOUNCING)
        self._source_audio("announcement", cue)
        self.trace("announcement-started")
        async def caller_notice():
            await self._play_chunks(_bytes(cue), kind="announcement")
            await self._ack("announcement")
        async def owner_notice():
            notice = await self.owner_cue_task
            router = self.controller.router(self.session.id)
            self.controller._note_cleared(self.session.id, router.set_owner_notice(True))
            try:
                await self._play_chunks(_bytes(notice), kind="announcement", role=OWNER)
                await self._ack("owner-notice", role=OWNER)
            finally:
                router.set_owner_notice(False)
        if self.owner_cue_task is not None:
            # Both private legs finish their own cue before the shared agent reply.
            async with asyncio.TaskGroup() as group:
                group.create_task(caller_notice())
                group.create_task(owner_notice())
        else:
            await caller_notice()
        self.trace("announcement-completed", reply_ready=self.preparation_task.done())

    async def _announcement(self, http, text=ANNOUNCEMENT):
        key = (self.snapshot.voice_id, self.voice.elevenlabs_model, self.voice.delivery.cache_key, text)
        cache = self.controller.announcement_cache
        if key in cache:
            self.trace("announcement-cache-hit")
            return cache[key]
        started = time.monotonic()
        audio = await cached_static_audio(self.controller, self.snapshot, text)
        self.trace("announcement-audio-ready", duration_ms=int((time.monotonic()-started)*1000))
        return audio

    async def _produce(self, http, conversation):
        if self.voicemail and self.voicemail_phase == "greeting":
            # A known invitation does not need to wait for Gemini. Keep the
            # ordinary playback/ack/transcript path so later Gemini turns see
            # only the greeting the caller actually heard.
            self.trace("voicemail-fixed-greeting")
            await self.phrases.put(VOICEMAIL_GREETING)
            await self.phrases.put(None)
            return
        # Preserve complete sentences in live speech. Voicemail keeps its
        # smaller bound for concise recaps; both paths protect numbers/times.
        buffer = (SentenceBuffer(limit=VOICEMAIL_PHRASE_CHARS) if self.voicemail
                  else SentenceBuffer())
        commands = ReplyCommandBuffer()
        chars = 0
        first_token = True
        # Voicemail has no human bridge to resume after a slow response. Bound
        # silence with a brief same-voice acknowledgement instead of a restart.
        first_text_seconds = VOICEMAIL_FIRST_TEXT_SECONDS if self.voicemail else FIRST_TEXT_SECONDS
        try:
            async for event in reply_events(http, self.voice.gemini_api_key,
                    conversation.system, deepcopy(conversation.contents),
                    model=self.voice.gemini_model, max_output_tokens=self.voice.max_reply_tokens,
                    timeout=self.voice.request_timeout, first_text_timeout=first_text_seconds,
                    retry_first_text_timeout=not self.voicemail, trace=True):
                if event["kind"] == "trace":
                    self.trace("gemini-attempt-" + event["stage"], **{
                        key: value for key, value in event.items() if key not in {"kind", "stage"}})
                if event["kind"] == "retry":
                    self.trace("gemini-response-retry", reason=event.get("reason", "unknown"))
                if event["kind"] == "text":
                    if first_token:
                        self.trace("gemini-first-text")
                        first_token = False
                    chars += len(event["text"])
                    if chars > MAX_REPLY_CHARS:
                        raise ValueError("Reply too long")
                    for phrase in buffer.feed(commands.feed(event["text"])):
                        await self.phrases.put(phrase)
        except (GeminiError, httpx.HTTPError, TimeoutError) as exc:
            if not self.voicemail or chars:
                raise
            if self.remote_revision != self.controller._remote_revisions.get(self.session.id, 0):
                # New speech was accepted while Gemini was still preparing.
                # Speaking the old snapshot would put an agent turn after that
                # correction, hiding it from the next local readback. Let the
                # listening policy wait for its endpoint and capture it afresh.
                self.end_deferred = True
                self.trace("voicemail-local-reply-deferred-for-new-speech", phase=self.voicemail_phase)
                await self.phrases.put(None)
                return
            # A model timeout must not turn a conversational voicemail into a
            # second mailbox. Acknowledge the captured message without parroting
            # it or pretending to summarize; a later turn can use Gemini again.
            recovery = recovery_reply(
                self.voicemail_phase, self.context,
                caller_requested_end=self.caller_requested_end)
            self.end_requested = recovery.end_requested
            if recovery.phase != self.voicemail_phase:
                self.voicemail_phase = recovery.phase
                voicemail = self.controller._voicemail_agents.get(self.session.id)
                if voicemail is not None:
                    voicemail.reframe_reply(recovery.phase)
            self.trace("voicemail-local-reply", phase=self.voicemail_phase,
                       reason=type(exc).__name__, end_requested=self.end_requested)
            await self.phrases.put(recovery.spoken)
            await self.phrases.put(None)
            return
        commands.finish()
        self.end_requested = commands.end_call
        trailing = buffer.flush()
        if trailing:
            await self.phrases.put(trailing)
        await self.phrases.put(None)
        self.trace("gemini-complete", end_requested=self.end_requested)

    async def _next_phrase(self):
        if self.held_phrase is not None:
            phrase, self.held_phrase = self.held_phrase, None
        elif self.phrases_ended:
            return None
        else:
            phrase = await self._next_raw_phrase()
        if phrase is None:
            self.phrases_ended = True
            return None
        # Prepare short adjacent sentences together for continuous prosody.
        # Long responses retain streaming and the next-phrase prefetch limit.
        if (not self.voicemail and self.voice.elevenlabs_model.startswith(("eleven_v3", "eleven_v4"))
                and len(phrase) < COALESCE_CHARS):
            if not self.producer.done():
                await asyncio.wait((self.producer,), timeout=COALESCE_SECONDS)
            while not self.phrases.empty():
                following = self.phrases.get_nowait()
                if following is None:
                    self.phrases_ended = True
                    break
                if len(phrase) + len(following) + 1 > COALESCE_CHARS:
                    self.held_phrase = following
                    break
                phrase += " " + following
        return phrase

    async def _next_raw_phrase(self):
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

    async def _speech(self, http, phrase, *, previous_text=None):
        started = time.monotonic()
        first = True
        stream = self.synthesis.speech_bytes(phrase, previous_text=previous_text)
        self.synthesis_sequence += 1
        phase = f"reply-{self.synthesis_sequence}"
        try:
            async for chunk in stream:
                self._source_audio(phase, chunk)
                if first and chunk:
                    self.trace("elevenlabs-first-audio", duration_ms=int((time.monotonic()-started)*1000))
                    first = False
                yield chunk
        finally:
            await stream.aclose()

    def _source_audio(self, phase, chunk):
        callback = getattr(self.controller, "source_audio", None)
        if callback is not None and chunk:
            try:
                callback(self.session.id, f"{self.epoch}:{phase}", chunk)
            except Exception:
                self.trace("source-audio-diagnostic-unavailable")

    async def _phrase(self, phrase, chunks):
        self.stage = "reply-playback"
        self.trace("reply-started")
        self.pending = {"text": phrase, "start": self.controller.elapsed_ms(self.session.id),
                        "frames": 0, "sent_before": self.controller._agent_frames_sent.get(self.session.id, 0)}
        await self._play_chunks(chunks, kind="agent")
        await self._ack("reply")
        if self.current():
            await self._record(self.pending, "played")
            self.confirmed.append(phrase)
            self.pending = None
            self.trace("reply-played")

    async def _play_chunks(self, chunks, *, kind, role=REMOTE):
        # A stream that keeps returning tiny chunks must not evade the timeout.
        async with asyncio.timeout(self.voice.request_timeout):
            await self._play_chunks_bounded(chunks, kind=kind, role=role)

    async def _play_chunks_bounded(self, chunks, *, kind, role=REMOTE):
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
                    await self._frame(frame, kind, role=role)
            if buffer:
                await self._frame(next(iter_frames(bytes(buffer))), kind, role=role)
            if size == 0:
                raise ValueError("No speech audio")
        finally:
            close = getattr(chunks, "aclose", None)
            if close:
                await close()

    async def _frame(self, frame, kind, *, role=REMOTE):
        router = self.controller.router(self.session.id)
        deadline = time.monotonic() + FRAME_STALL_SECONDS
        while router.pending_agent(role) >= AGENT_QUEUE_FRAMES:
            if not self.current():
                raise asyncio.CancelledError
            if time.monotonic() >= deadline:
                raise TimeoutError("Audio writer stalled")
            await asyncio.sleep(0.01)
        if not self.current():
            raise asyncio.CancelledError
        sent = (router.send_announcement(frame, role=role) if kind == "announcement"
                else router.send_agent((frame,), reply_epoch=self.epoch))
        if not sent:
            raise ValueError("Remote stream unavailable")
        if kind == "agent" and self.pending is not None:
            self.pending["frames"] += 1

    async def _ack(self, kind, *, role=REMOTE):
        self.sequence += 1
        name = f"{kind}-{self.epoch}-{self.sequence}"
        await self.controller.wait_for_mark(self.session.id, name,
            timeout=PLAYBACK_ACK_SECONDS + 1.0, role=role)
        self.trace("playback-acknowledged", kind=kind)
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
