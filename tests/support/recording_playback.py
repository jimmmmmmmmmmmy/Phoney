"""Shared fixtures and fakes for focused integration checks."""

import asyncio


from datetime import datetime, timedelta, timezone


import io


import json


import os


import struct


from types import SimpleNamespace


import wave


import pytest


from starlette.applications import Starlette


from starlette.requests import Request


from starlette.responses import JSONResponse


from starlette.routing import Route


from starlette.testclient import TestClient


from media_capture import playback


from media_capture.playback import RecordingLibrary


CALL = "CA" + "1" * 32


STREAM = "MZ" + "2" * 32


def write_capture(root, *, sid=CALL, inbound=(100, -200, 300, -400), outbound=(11, 22),
                  status="completed", minute=0):
    directory = root / sid
    directory.mkdir(parents=True)
    tracks = {}
    for name, samples in (("inbound", inbound), ("outbound", outbound)):
        with wave.open(str(directory / (name + ".wav")), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(struct.pack("<" + "h" * len(samples), *samples))
        tracks[name] = {"file": name + ".wav", "samples": len(samples),
                        "meaning": "caller-input" if name == "inbound" else "caller-playback"}
    started = datetime(2026, 9, 26, tzinfo=timezone.utc) + timedelta(minutes=minute)
    manifest = {"schema_version": 1, "call_sid": sid, "stream_sid": STREAM,
                "account_sid": "AC" + "3" * 32, "started_at": started.isoformat(),
                "finished_at": (started + timedelta(seconds=10)).isoformat(), "status": status,
                "sample_rate": 8000, "channels": 1, "sample_width": 2, "encoding": "pcm_s16le",
                "tracks": tracks, "counters": {"dropped_messages": 0, "rejected_messages": 0}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory, manifest


def library(root, *, enabled=True):
    return RecordingLibrary(SimpleNamespace(media_storage_dir=str(root), media_capture_enabled=enabled))


def request(method="GET", **headers):
    return Request({"type": "http", "method": method,
                    "headers": [(key.encode(), value.encode()) for key, value in headers.items()]})


def client_for(lib):
    async def catalog(req):
        return JSONResponse(await asyncio.to_thread(lib.snapshot))
    async def audio(req):
        return await asyncio.to_thread(lib.response, req.path_params["sid"],
                                       req.query_params.get("track", "combined"), req)
    return TestClient(Starlette(routes=[Route("/api/recordings", catalog),
        Route("/api/recordings/{sid}/audio", audio, methods=["GET", "HEAD"])]))
