"""Bounded listening policy for the bridge's single-caller voicemail agent.

The model never decides whether a pause happened. Final STT, actual playback
completion, and these timers choose a trusted prompt phase. Recording and
transcript storage stay on the existing canonical call pipeline.
"""
from __future__ import annotations

import asyncio
import inspect


class VoicemailAgent:
    """Schedule replies without dialing, synthesizing, or fabricating messages.

    ``on_reply(phase)`` starts one normal DialogueRun; ``on_end(reason)`` ends
    the existing call. The controller reports playback via reply_completed,
    not merely Gemini completion. Call transcript after filtering duplicates,
    stale pre-playback STT, and non-caller tracks.
    """

    def __init__(self, session, *, on_reply, on_end, pause_seconds=2.0,
                 initial_silence_seconds=20.0, confirmation_silence_seconds=15.0,
                 capture_seconds=60.0, total_seconds=180.0):
        self.session = session
        self.on_reply, self.on_end = on_reply, on_end
        self.pause_seconds = float(pause_seconds)
        self.initial_silence_seconds = float(initial_silence_seconds)
        self.confirmation_silence_seconds = float(confirmation_silence_seconds)
        self.capture_seconds, self.total_seconds = float(capture_seconds), float(total_seconds)
        self.phase = "greeting"
        self.busy = True
        self.closed = False
        self.pending_final = False
        self.utterance_open = False
        self.has_message = False
        self.activity_version = 0
        self._timers = {}

    def start(self):
        """Start the hard session bound; caller greeting is started by controller."""
        if not self.closed and "total" not in self._timers:
            self._arm("total", self.total_seconds, self._deadline)

    def _active(self):
        return not self.closed and self.session.active and self.session.voicemail

    async def _invoke(self, callback, *args):
        result = callback(*args)
        if inspect.isawaitable(result):
            await result

    def _cancel(self, name):
        task = self._timers.pop(name, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _arm(self, name, delay, callback):
        self._cancel(name)

        async def later():
            try:
                await asyncio.sleep(delay)
                if self._active():
                    await callback()
            finally:
                if self._timers.get(name) is asyncio.current_task():
                    self._timers.pop(name, None)

        self._timers[name] = asyncio.create_task(later())

    def reply_started(self, phase):
        """Claim pending finalized text for this reply and pause listening timers."""
        if not self._active():
            return
        self.phase = str(phase)
        self.session.voicemail_phase = self.phase
        self.busy = True
        self.pending_final = False
        for name in ("pause", "silence", "capture"):
            self._cancel(name)

    async def reply_completed(self, phase=None):
        """Advance only once a reply was heard, including its final playback mark."""
        if not self._active():
            return
        phase = str(phase or self.phase)
        if phase != self.phase:
            return  # A canceled reply cannot advance a newer dialogue phase.
        self.busy = False
        if phase in {"no_message", "unconfirmed", "complete"}:
            await self._finish("voicemail-" + phase.replace("_", "-"))
            return
        if phase in {"readback", "confirm"}:
            self.has_message = True
        self._listen()

    def interrupted(self):
        """A caller interrupted audible agent speech; wait for the caller's turn."""
        if not self._active():
            return
        self.busy = False
        self._listen()

    def suspend(self):
        """Keep the hard deadline during transport recovery, but stop local replies."""
        was_busy = self.busy
        self.busy = True
        for name in ("pause", "silence", "capture"):
            self._cancel(name)
        return was_busy

    def resume_listening(self):
        if self._active():
            self.busy = False
            self._listen()

    def transcript(self, text, *, final=True, activity=False, speech_final=None,
                   speech_started=False):
        """Collect final chunks, but start the pause only at an actual endpoint.

        ``None`` preserves callers without endpoint metadata. Production STT
        supplies explicit booleans, including empty end-of-utterance events.
        VAD activity can extend listening but cannot invent a recorded message.
        """
        text = str(text or "").strip()
        activity = bool(activity or speech_started)
        if not self._active() or (not text and not activity and speech_final is not True):
            return
        self.activity_version += 1
        if speech_final is True or (speech_final is None and final and text):
            self.utterance_open = False
        elif speech_final is False and (text or activity):
            self.utterance_open = True
        if final and text:
            self.pending_final = True
        self._cancel("pause")
        self._cancel("silence")
        if not self.busy:
            self._listen()

    def _listen(self):
        if not self._active() or self.busy:
            return
        if "capture" not in self._timers:
            self._arm("capture", self.capture_seconds, self._capture_expired)
        if self.pending_final and self.utterance_open:
            # An older finalized chunk is not proof that the current utterance
            # ended. The capture/session bounds still prevent an endless wait.
            return
        if self.pending_final:
            version = self.activity_version

            async def quiet():
                if (not self.busy and self.pending_final and not self.utterance_open
                        and self.activity_version == version):
                    await self._request("confirm" if self.has_message else "readback")

            self._arm("pause", self.pause_seconds, quiet)
        else:
            delay = (self.confirmation_silence_seconds if self.has_message
                     else self.initial_silence_seconds)
            self._arm("silence", delay, self._silence_expired)

    async def _request(self, phase):
        if not self._active() or self.busy:
            return
        self.reply_started(phase)
        try:
            await self._invoke(self.on_reply, phase)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._finish("voicemail-reply-unavailable")

    async def _silence_expired(self):
        await self._request("unconfirmed" if self.has_message else "no_message")

    async def _capture_expired(self):
        if self.pending_final:
            await self._request("confirm" if self.has_message else "readback")
        else:
            # No finalized text means there is nothing safe to repeat back.
            # The model must not invent a message from silence or an interim.
            await self._request("unconfirmed" if self.has_message else "no_message")

    async def _deadline(self):
        await self._finish("voicemail-time-limit")

    async def _finish(self, reason):
        if not self._active():
            return
        self.close()
        await self._invoke(self.on_end, reason)

    def close(self):
        """Invalidate timers immediately on caller hangup, failure, or shutdown."""
        self.closed = True
        for name in tuple(self._timers):
            self._cancel(name)
