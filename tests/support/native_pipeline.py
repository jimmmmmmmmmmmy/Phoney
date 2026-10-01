"""Shared fixtures and fakes for focused integration checks."""

import asyncio


from datetime import datetime, timezone


import json


from types import SimpleNamespace


import wave


import pytest


from bridge_pipeline import BridgePipeline


from call_details import CallDetailsStore


from media_capture import CaptureManager


from media_capture.capture import decode_mulaw


from transcription import TranscriptionManager


from support.transcription import Connector, result, settings, until


CALL = "CA" + "a" * 32


STREAM = "MZ" + "b" * 32


class Detection:
    def __init__(self):
        self.starts, self.frames, self.ends = [], [], []

    def start(self, *args):
        self.starts.append(args)

    def offer(self, *args):
        self.frames.append(args)

    def finish(self, *args):
        self.ends.append(args)


def session():
    return SimpleNamespace(id="native-session", canonical_call_sid=CALL,
        native_conference=True, native_owner_muted=False, phase="connected",
        to="+12025550101", created_at=datetime.now(timezone.utc).isoformat(), legs={
            "remote": SimpleNamespace(stream_sid=STREAM, generation=1),
            "owner": SimpleNamespace(stream_sid="MZ" + "c" * 32, generation=1)})


def audio_sent(socket):
    return b"".join(item for item in socket.sent if isinstance(item, bytes))
