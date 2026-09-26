"""Serial post-call summaries; provider and disk work never run in audio callbacks."""

import asyncio
from copy import deepcopy
import inspect
import math
import re
import time

from call_details import MAX_SUMMARY_ATTEMPTS, SUMMARY_KINDS, transcript_fingerprint

POLL_SECONDS = 2.0
REQUEST_SECONDS = 45.0
STORE_SECONDS = 5.0
CLOSE_SECONDS = 6.0
MAX_SESSIONS = 20
MAX_ATTEMPTS = MAX_SUMMARY_ATTEMPTS
# A brief provider outage should not permanently strand an otherwise valid
# transcript. The durable budget still prevents an unbounded background loop.
RETRY_DELAYS = (30, 120, 600, 1800)
RETRYABLE_ERRORS = {"rate_limited", "provider_unavailable", "timeout", "transport_error",
                    "provider-timeout", "interrupted"}


class SummaryManager:
    def __init__(self, settings, transcription, call_details, active_call_ids=lambda: set(), provider=None,
                 can_run=lambda: True):
        self.settings = settings
        self.transcription = transcription
        self.call_details = call_details
        self.active_call_ids = active_call_ids
        self.can_run = can_run
        self.enabled = bool(getattr(settings, "gemini_api_key", "")
                            and getattr(settings, "transcription_enabled", False)
                            and getattr(settings, "call_details_storage_dir", ""))
        if self.enabled and provider is None:
            from gemini_summary import GeminiSummarizer
            provider = GeminiSummarizer(settings)
        self.provider = provider
        self._lock = asyncio.Lock()
        self._runner = None
        self._running_task = None
        self._io_tasks = set()
        self._busy = False
        self._closed = False
        self._provider_closed = False

    @property
    def active_count(self):
        # An idle polling timer must not prevent deployment from draining.
        return int(self._busy or any(not task.done() for task in self._io_tasks))

    def start(self):
        if self.enabled and not self._closed and (self._runner is None or self._runner.done()):
            self._runner = asyncio.create_task(self._run(), name="post-call-summaries")

    async def _run(self):
        while not self._closed:
            await self.run_once()
            await asyncio.sleep(POLL_SECONDS)

    async def _store(self, method, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
        self._io_tasks.add(task)
        def done(finished):
            self._io_tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()  # Retrieve failures without logging private data.
        task.add_done_callback(done)
        return await asyncio.wait_for(asyncio.shield(task), STORE_SECONDS)

    def _documents(self):
        try:
            snapshot = self.transcription.snapshot()
            sessions = snapshot.get("sessions") if isinstance(snapshot, dict) else None
            if not isinstance(sessions, list):
                return []
            return [deepcopy(document) for document in sessions[:MAX_SESSIONS]
                    if isinstance(document, dict) and transcript_fingerprint(document)]
        except Exception:
            return []

    def _inactive(self, call_sid):
        try:
            active = self.active_call_ids()
            return isinstance(active, (set, frozenset, list, tuple)) and call_sid not in active
        except Exception:
            return False

    def _allowed(self):
        try:
            return not self._closed and self.can_run() is True
        except Exception:
            return False

    def _current(self, call_sid, fingerprint):
        if not self._inactive(call_sid):
            return None
        return next((document for document in self._documents()
                     if document["call_sid"] == call_sid
                     and transcript_fingerprint(document) == fingerprint), None)

    async def _state(self, document, kind):
        result = await self._store(self.call_details.summary_state, document["call_sid"], document, kind=kind)
        if (not isinstance(result, dict) or result.get("status") not in ("missing", "pending", "completed", "failed")
                or type(result.get("attempts")) is not int or not 0 <= result["attempts"] <= MAX_ATTEMPTS
                or type(result.get("retry_at")) not in (int, float)
                or not math.isfinite(result["retry_at"]) or result["retry_at"] < 0):
            return None
        return result

    async def _failed(self, document, error, retry_at=0, *, kind):
        safe_error = error if isinstance(error, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", error) else "provider-error"
        try:
            await self._store(self.call_details.fail_summary, document["call_sid"], document,
                              safe_error, retry_at=retry_at, kind=kind)
        except Exception:
            pass  # A persisted pending attempt is still bounded on restart.

    async def run_once(self):
        """Attempt one detailed or brief job, serially and independently."""
        if (not self.enabled or not self._allowed() or self._lock.locked()
                or any(not task.done() for task in self._io_tasks)):
            return
        async with self._lock:
            self._busy = True
            self._running_task = asyncio.current_task()
            try:
                if not self._allowed():
                    return
                for document in self._documents():
                    if not self._inactive(document["call_sid"]):
                        continue
                    for kind in SUMMARY_KINDS:
                        state = await self._state(document, kind)
                        if not state or state["status"] == "completed":
                            continue
                        if (state["status"] == "failed" and not state["retry_at"]
                                and 0 < state["attempts"] < MAX_ATTEMPTS
                                and state.get("error") in RETRYABLE_ERRORS):
                            # Recover older, shorter retry budgets without
                            # resetting counts or sending requests on startup.
                            await self._failed(document, state["error"],
                                               time.time() + RETRY_DELAYS[state["attempts"] - 1], kind=kind)
                            continue
                        if state["status"] == "failed" and (not state["retry_at"] or state["retry_at"] > time.time()):
                            continue
                        if state["attempts"] >= MAX_ATTEMPTS:
                            if state["status"] == "pending":
                                await self._failed(document, "retry-exhausted", kind=kind)
                            continue
                        await self._summarize(document, state["attempts"] + 1, kind)
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                # Metadata or provider initialization errors cannot escape into
                # the call server; the next poll can re-evaluate durable state.
                pass
            finally:
                self._busy = False
                self._running_task = None

    async def _summarize(self, document, attempt, kind):
        sid = document["call_sid"]
        fingerprint = transcript_fingerprint(document)
        begun = False
        try:
            if not self._allowed() or not self._current(sid, fingerprint):
                return
            begun = bool(await self._store(self.call_details.begin_summary, sid, document, kind=kind))
            if not begun:
                return  # Never spend on an attempt that was not durably counted.
            if not self._allowed():
                retry_at = time.time() + RETRY_DELAYS[attempt - 1] if attempt < MAX_ATTEMPTS else 0
                await self._failed(document, "interrupted", retry_at, kind=kind)
                return
            if not self._current(sid, fingerprint):
                await self._failed(document, "transcript-changed", kind=kind)
                return
            try:
                generate = self.provider.summarize_brief if kind == "brief" else self.provider.summarize
                generated = await asyncio.wait_for(generate(document), REQUEST_SECONDS)
            except asyncio.TimeoutError:
                retry_at = time.time() + RETRY_DELAYS[attempt - 1] if attempt < MAX_ATTEMPTS else 0
                await self._failed(document, "provider-timeout", retry_at, kind=kind)
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                retryable = getattr(error, "retryable", False) is True
                retry_at = time.time() + RETRY_DELAYS[attempt - 1] if retryable and attempt < MAX_ATTEMPTS else 0
                await self._failed(document, getattr(error, "code", "provider-error"), retry_at, kind=kind)
                return
            if not isinstance(generated, str) or not generated.strip() or len(generated) > SUMMARY_KINDS[kind][2]:
                await self._failed(document, "invalid-response", kind=kind)
                return
            current = self._current(sid, fingerprint)
            if current is None:
                await self._failed(document, "transcript-changed", kind=kind)
                return
            state = await self._state(current, kind)
            if not state or state["status"] == "completed":
                return  # An operator may have written a summary while we waited.
            # Recheck after disk I/O as well; transcript finalization may have
            # changed while the worker was checking an existing agent summary.
            current = self._current(sid, fingerprint)
            if current is None:
                await self._failed(document, "transcript-changed", kind=kind)
                return
            saved = await self._store(self.call_details.set_summary, sid, generated.strip(), current,
                                     source="gemini", model=getattr(self.settings, "gemini_summary_model", None), kind=kind)
            if not saved:
                await self._failed(document, "storage-error", kind=kind)
        except asyncio.CancelledError:
            if begun:
                retry_at = time.time() + RETRY_DELAYS[attempt - 1] if attempt < MAX_ATTEMPTS else 0
                await self._failed(document, "interrupted", retry_at, kind=kind)
            raise
        except Exception:
            if begun:
                await self._failed(document, "storage-error", kind=kind)

    async def close(self):
        self._closed = True
        tasks = {task for task in (self._runner, self._running_task)
                 if task is not None and task is not asyncio.current_task() and not task.done()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=CLOSE_SECONDS)
        if self._io_tasks:
            await asyncio.wait(tuple(self._io_tasks), timeout=STORE_SECONDS)
        if self.provider is not None and not self._provider_closed:
            self._provider_closed = True
            try:
                close = getattr(self.provider, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, CLOSE_SECONDS)
            except Exception:
                pass
