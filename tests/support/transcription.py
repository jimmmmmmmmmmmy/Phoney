"""Shared fixtures and fakes for focused integration checks."""

import asyncio


import json


from types import SimpleNamespace


ACCOUNT = "AC" + "a" * 32


def settings(path, **changes):
    return SimpleNamespace(**(dict(transcription_enabled=True, deepgram_api_key="fixture-private-key",
                                  deepgram_model="nova-3", transcript_storage_dir=str(path),
                                  media_max_seconds=1800, account_sid=ACCOUNT,
                                  media_capture_enabled=True, media_storage_dir=str(path / "audio")) | changes))


def result(text="Hello there.", *, final=True, start=0, duration=0.02, confidence=0.98):
    return {"type": "Results", "start": start, "duration": duration, "is_final": final,
            "channel": {"alternatives": [{"transcript": text, "confidence": confidence}]}}


class Provider:
    def __init__(self, *, failure=False, stall=False, tail=None, close_result=True):
        self.messages = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.failure = failure
        self.stall = stall
        self.close_result = close_result
        self.tail = tail

    async def __aenter__(self):
        if self.failure:
            raise RuntimeError("Never expose fixture-private-key")
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def send(self, data):
        if self.stall:
            await asyncio.Event().wait()
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data)["type"] == "CloseStream" and self.close_result:
            if self.tail:
                self.messages.put_nowait(json.dumps(self.tail))
            self.messages.put_nowait(json.dumps({"type": "Metadata"}))
            self.messages.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return message

    def push(self, event):
        self.messages.put_nowait(json.dumps(event))


class Connector:
    def __init__(self, options=None):
        self.sockets = []
        self.requests = []
        self.options = options or [{}, {}]

    def __call__(self, url, **kwargs):
        self.requests.append((url, kwargs))
        socket = Provider(**self.options[len(self.sockets) % len(self.options)])
        self.sockets.append(socket)
        return socket


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)
