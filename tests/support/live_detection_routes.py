"""Shared fixtures and fakes for focused integration checks."""

import asyncio


from dataclasses import replace


import json


import pytest


from fastapi.testclient import TestClient


from app import create_app


from support.media_webhooks import (
    Gateway,
    PARENT,
    SETTINGS,
    assert_socket_closed,
    send_audio,
    send_start,
    socket_headers,
    stop_message,
    stream_and_token,
)


from support.transcription import until


class DetectionSocket:
    def __init__(self):
        self.sent = []
        self.ended = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def send(self, message):
        self.sent.append(message)
        if message == "":
            self.ended.set()

    def __aiter__(self):
        return self.messages()

    async def messages(self):
        await self.ended.wait()
        duration = sum(len(item) for item in self.sent if isinstance(item, bytes)) // 16
        yield json.dumps({"type": "frame", "frame": {
            "start_time_ms": 0, "end_time_ms": duration,
            "verdict": "non-synthetic", "confidence": 0.94,
        }})
        yield json.dumps({"type": "done", "duration_ms": duration, "frame_count": 1})


class DetectionConnector:
    def __init__(self):
        self.urls = []
        self.sockets = []

    def __call__(self, url):
        self.urls.append(url)
        socket = DetectionSocket()
        self.sockets.append(socket)
        return socket
