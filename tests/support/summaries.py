"""Shared fixtures and fakes for focused integration checks."""

import asyncio


from copy import deepcopy


import json


import threading


from types import SimpleNamespace


from call_details import transcript_fingerprint


from summaries import SummaryManager


import summaries


CALL = "CA" + "1" * 32


def document():
    return {"call_sid": CALL, "stream_sid": "MZ" + "2" * 32, "status": "completed",
            "started_at": "2026-09-26T10:00:00+00:00", "ended_at": "2026-09-26T10:01:00+00:00",
            "segments": [{"id": "inbound-0", "track": "inbound", "start_ms": 0, "end_ms": 1000,
                          "text": "Fixture callback request.", "confidence": .9}]}


def settings(**changes):
    return SimpleNamespace(**({"gemini_api_key": "fixture-key", "transcription_enabled": True,
        "call_details_storage_dir": "/fixture/details", "gemini_summary_model": "gemini-3.8-flash"} | changes))


class Transcription:
    def __init__(self, documents=None): self.documents = documents if documents is not None else [document()]
    def snapshot(self): return {"sessions": deepcopy(self.documents)}


class ProviderError(Exception):
    def __init__(self, code, retryable=False):
        self.code, self.retryable = code, retryable


class Provider:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.brief_calls = []
        self.started = asyncio.Event()
        self.gate = None
        self.concurrent = self.maximum = self.closed = 0
    async def summarize(self, doc):
        self.calls.append(deepcopy(doc))
        self.concurrent += 1
        self.maximum = max(self.maximum, self.concurrent)
        self.started.set()
        try:
            if self.gate: await self.gate.wait()
            outcome = self.outcomes.pop(0) if self.outcomes else "Caller requested a callback."
            if isinstance(outcome, Exception): raise outcome
            return outcome
        finally:
            self.concurrent -= 1
    async def summarize_brief(self, doc):
        self.brief_calls.append(deepcopy(doc))
        return "Caller requested a callback."
    async def close(self): self.closed += 1


class Store:
    """Tiny durable fake for worker tests; real store has separate schema tests."""
    def __init__(self, path=None):
        self.path = path
        self.jobs = json.loads(path.read_text()) if path and path.exists() else {}
        self.saved = {}
        self.state_calls = 0
        self.begin_result = self.save_result = True
        self.after_begin = None
        self.save_started = threading.Event()
        self.save_gate = None
    def _persist(self):
        if self.path: self.path.write_text(json.dumps(self.jobs))
    def summary_state(self, sid, doc, *, kind="detailed"):
        key = sid if kind == "detailed" else sid + "/brief"
        self.state_calls += 1
        fingerprint = transcript_fingerprint(doc)
        if self.saved.get(key, {}).get("fingerprint") == fingerprint:
            return {"status": "completed", "attempts": 0, "retry_at": 0, "error": ""}
        job = self.jobs.get(key, {})
        if job.get("fingerprint") == fingerprint: return deepcopy(job)
        return {"status": "missing", "attempts": 0, "retry_at": 0, "error": ""}
    def begin_summary(self, sid, doc, *, kind="detailed"):
        key = sid if kind == "detailed" else sid + "/brief"
        if not self.begin_result: return False
        state = self.summary_state(sid, doc, kind=kind)
        if state["status"] == "completed" or state["attempts"] >= summaries.MAX_ATTEMPTS: return False
        self.jobs[key] = {"status": "pending", "attempts": state["attempts"] + 1, "retry_at": 0,
                          "error": "", "fingerprint": transcript_fingerprint(doc)}
        self._persist()
        if self.after_begin: self.after_begin()
        return True
    def fail_summary(self, sid, doc, error, retry_at=0, *, kind="detailed"):
        key = sid if kind == "detailed" else sid + "/brief"
        if self.jobs.get(key, {}).get("fingerprint") != transcript_fingerprint(doc): return False
        self.jobs[key].update(status="failed", error=error, retry_at=retry_at)
        self._persist()
        return True
    def set_summary(self, sid, text, doc, source="agent", model=None, *, kind="detailed"):
        key = sid if kind == "detailed" else sid + "/brief"
        self.save_started.set()
        if self.save_gate: self.save_gate.wait(timeout=2)
        if not self.save_result: return False
        self.saved[key] = {"text": text, "source": source, "model": model,
                           "fingerprint": transcript_fingerprint(doc)}
        return True
