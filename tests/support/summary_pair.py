"""Shared fixtures and fakes for focused integration checks."""

import asyncio


from copy import deepcopy


import json


from types import SimpleNamespace


from call_details import CallDetailsStore


from summaries import SummaryManager


CALL = "CA" + "a" * 32


DETAILED = "Caller requested an afternoon callback. New College DS agreed to call tomorrow at 2 PM."


BRIEF = "New College DS will call back tomorrow at 2 PM."


DISPLAY_DETAILED = "Caller requested an afternoon callback. James agreed to call tomorrow at 2 PM."


DISPLAY_BRIEF = "James will call back tomorrow at 2 PM."


def document():
    return {
        "call_sid": CALL, "stream_sid": "MZ" + "b" * 32,
        "status": "completed", "started_at": "2026-09-26T10:00:00+00:00",
        "ended_at": "2026-09-26T10:01:00+00:00",
        "segments": [
            {"id": "inbound-0", "track": "inbound", "start_ms": 0,
             "end_ms": 1000, "text": "Please call tomorrow at 2 PM."},
            {"id": "outbound-0", "track": "outbound", "start_ms": 1200,
             "end_ms": 2200, "text": "We will call tomorrow at 2 PM."},
        ],
    }


class Transcription:
    def __init__(self):
        self.documents = [document()]

    def snapshot(self):
        return {"sessions": deepcopy(self.documents)}


class DualProvider:
    def __init__(self, *, detailed=(), brief=()):
        self.outcomes = {"detailed": list(detailed), "brief": list(brief)}
        self.calls = []
        self.started = {kind: asyncio.Event() for kind in self.outcomes}
        self.gates = {}
        self.concurrent = self.maximum = self.closed = 0

    async def _generate(self, kind, doc):
        self.calls.append((kind, deepcopy(doc)))
        self.concurrent += 1
        self.maximum = max(self.maximum, self.concurrent)
        self.started[kind].set()
        try:
            if kind in self.gates:
                await self.gates[kind].wait()
            outcome = (self.outcomes[kind].pop(0) if self.outcomes[kind]
                       else DETAILED if kind == "detailed" else BRIEF)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.concurrent -= 1

    async def summarize(self, doc):
        return await self._generate("detailed", doc)

    async def summarize_brief(self, doc):
        return await self._generate("brief", doc)

    async def close(self):
        self.closed += 1


def manager_for(tmp_path, store, provider, transcription=None, **kwargs):
    settings = SimpleNamespace(
        gemini_api_key="fixture-only", transcription_enabled=True,
        call_details_storage_dir=str(tmp_path), gemini_summary_model="gemini-3.8-flash",
    )
    return SummaryManager(settings, transcription or Transcription(), store,
                          provider=provider, **kwargs)


def disk_record(tmp_path):
    return json.loads((tmp_path / (CALL + ".json")).read_text())


def public_record(store, doc=None):
    return store.snapshot([doc or document()])["calls"][0]
