"""Capture lifecycle, bounded audio processing, and filesystem isolation."""

import asyncio
import base64
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import wave

import pytest

from media_capture import CaptureManager, CaptureRejected
from media_capture.capture import decode_mulaw

CALL = "CA" + "1" * 32
STREAM = "MZ" + "2" * 32
ACCOUNT = "AC" + "a" * 32


def settings(path, **changes):
    return SimpleNamespace(**(dict(account_sid=ACCOUNT, media_capture_enabled=True,
                                  media_storage_dir=str(path), media_max_seconds=1_800) | changes))


def start(ticket, **changes):
    fields = dict(accountSid=ACCOUNT, callSid=CALL, streamSid=STREAM,
                  tracks=["inbound", "outbound"], customParameters={"token": ticket.token},
                  mediaFormat={"encoding": "audio/x-mulaw", "sampleRate": 8_000, "channels": 1})
    fields.update(changes)
    return {"event": "start", "streamSid": STREAM, "start": fields}


def media(track="inbound", chunk=1, timestamp=0, payload=b"\xff" * 160, **changes):
    fields = dict(track=track, chunk=str(chunk), timestamp=str(timestamp),
                  payload=base64.b64encode(payload).decode())
    fields.update(changes)
    return {"event": "media", "streamSid": STREAM, "media": fields}


def stop():
    return {"event": "stop", "streamSid": STREAM,
            "stop": {"accountSid": ACCOUNT, "callSid": CALL}}


class Socket:
    def __init__(self, events=()):
        self.messages = asyncio.Queue()
        for event in events:
            self.messages.put_nowait({"type": "websocket.receive", "text": json.dumps(event)})
        self.accepted = False
        self.closed = None
        self.sent_audio = []

    async def accept(self):
        self.accepted = True

    async def receive(self):
        return await self.messages.get()

    async def close(self, code=1000):
        if self.closed is None:
            self.closed = code
            self.messages.put_nowait({"type": "websocket.disconnect"})


def read_manifest(path):
    return json.loads((path / CALL / "manifest.json").read_text())


def read_wav(path, track):
    with wave.open(str(path / CALL / f"{track}.wav"), "rb") as file:
        assert (file.getnchannels(), file.getsampwidth(), file.getframerate()) == (1, 2, 8_000)
        return file.readframes(file.getnframes())


def test_mulaw_reference_values_and_zero():
    assert decode_mulaw(bytes([0, 128, 255, 127])) == b"\x84\x82\x7c\x7d\x00\x00\x00\x00"


def test_two_tracks_keep_timestamps_and_private_artifacts(tmp_path):
    async def run():
        storage = tmp_path / "captures"
        manager = CaptureManager(settings(storage))
        ticket = manager.reserve(CALL)
        assert manager.reserve(CALL) is ticket
        assert manager.pending_count == 1 and manager.active_count == 0
        assert not storage.exists()
        socket = Socket([start(ticket), media(payload=b"\x00" * 160),
                         media(chunk=2, timestamp=40, payload=b"\x80" * 160),
                         media("outbound", timestamp=20), stop()])
        await manager.handle(socket, CALL)
        manifest = read_manifest(storage)
        assert manifest["status"] == "completed"
        assert manifest["finish_reason"] == "stream-stopped"
        assert manifest["tracks"]["inbound"]["samples"] == 480
        assert manifest["tracks"]["inbound"]["gap_samples"] == 160
        assert manifest["tracks"]["outbound"]["samples"] == 320
        assert manifest["tracks"]["outbound"]["meaning"] == "caller-playback"
        assert read_wav(storage, "inbound") == b"\x84\x82" * 160 + b"\x00\x00" * 160 + b"\x7c\x7d" * 160
        assert read_wav(storage, "outbound") == b"\x00\x00" * 320
        assert ticket.token not in (storage / CALL / "manifest.json").read_text()
        assert os.stat(storage).st_mode & 0o777 == 0o700
        assert os.stat(storage / CALL).st_mode & 0o777 == 0o700
        assert all(os.stat(file).st_mode & 0o777 == 0o600 for file in (storage / CALL).iterdir())
        assert manager.active_count == manager.pending_count == 0
        with pytest.raises(CaptureRejected):
            manager.reserve(CALL)
        await manager.close()
    asyncio.run(run())


@pytest.mark.parametrize("changes", [
    {"accountSid": "AC" + "b" * 32}, {"callSid": "CA" + "2" * 32},
    {"streamSid": "../../outside"}, {"customParameters": {"token": "wrong"}},
    {"customParameters": []}, {"tracks": ["inbound"]}, {"tracks": [["inbound"], "outbound"]},
    {"mediaFormat": {"encoding": "audio/pcm", "sampleRate": 8_000, "channels": 1}},
    {"mediaFormat": []},
])
def test_invalid_start_cannot_create_artifacts(tmp_path, changes):
    async def run():
        storage = tmp_path / "captures"
        manager = CaptureManager(settings(storage))
        ticket = manager.reserve(CALL)
        socket = Socket([start(ticket, **changes)])
        await manager.handle(socket, CALL)
        assert socket.closed == 1008
        assert not storage.exists()
        await manager.close()
    asyncio.run(run())


def test_duplicate_chunks_overlap_and_malformed_frames_do_not_corrupt_pcm(tmp_path):
    async def run():
        manager = CaptureManager(settings(tmp_path))
        ticket = manager.reserve(CALL)
        socket = Socket([start(ticket), media(), media(), media(chunk=2, timestamp=0),
                         media(chunk=3, timestamp=40, payload=b"\x00" * 160),
                         media(chunk=4, timestamp=60, payload=b"x", track="unknown"),
                         {"event": "media", "streamSid": STREAM, "media": []},
                         {"event": "stop", "streamSid": STREAM, "stop": []}, stop()])
        await manager.handle(socket, CALL)
        manifest = read_manifest(tmp_path)
        assert manifest["tracks"]["inbound"]["frames"] == 2
        assert manifest["tracks"]["inbound"]["samples"] == 480
        assert manifest["counters"]["rejected_messages"] == 5
    asyncio.run(run())


@pytest.mark.parametrize("timestamp,reason", [(1_000, "duration-limit")])
def test_duration_and_timestamp_jump_stop_capture_only(tmp_path, timestamp, reason):
    async def run():
        maximum = 1 if reason == "duration-limit" else 100
        manager = CaptureManager(settings(tmp_path, media_max_seconds=maximum))
        ticket = manager.reserve(CALL)
        socket = Socket([start(ticket), media(timestamp=timestamp)])
        await asyncio.wait_for(manager.handle(socket, CALL), timeout=2)
        manifest = read_manifest(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["finish_reason"] == reason
        assert len(read_wav(tmp_path, "inbound")) == 0
        assert socket.closed == 1000
    asyncio.run(run())


def test_disconnect_finalizes_partial_wavs(tmp_path):
    async def run():
        manager = CaptureManager(settings(tmp_path))
        ticket = manager.reserve(CALL)
        socket = Socket([start(ticket), media()])
        socket.messages.put_nowait({"type": "websocket.disconnect"})
        await manager.handle(socket, CALL)
        assert read_manifest(tmp_path)["status"] == "partial"
        assert len(read_wav(tmp_path, "inbound")) == 320
    asyncio.run(run())


def test_call_ended_grace_accepts_trailing_frames_and_rejects_restart(tmp_path):
    async def run():
        manager = CaptureManager(settings(tmp_path))
        ticket = manager.reserve(CALL)
        socket = Socket([start(ticket), media()])
        receive = asyncio.create_task(manager.handle(socket, CALL))
        while manager.active_count == 0:
            await asyncio.sleep(0)
        finish = asyncio.create_task(manager.finish(CALL))
        await asyncio.sleep(0.01)
        socket.messages.put_nowait({"type": "websocket.receive", "text": json.dumps(media(chunk=2, timestamp=20))})
        await finish
        await receive
        assert read_manifest(tmp_path)["finish_reason"] == "call-ended"
        assert read_manifest(tmp_path)["tracks"]["inbound"]["frames"] == 2
        assert manager.active_count == manager.pending_count == 0
        with pytest.raises(CaptureRejected):
            manager.reserve(CALL)
    asyncio.run(run())


def test_ended_before_reserve_cannot_start_and_disabled_does_not_reserve(tmp_path):
    async def run():
        manager = CaptureManager(settings(tmp_path))
        await manager.finish(CALL)
        with pytest.raises(CaptureRejected):
            manager.reserve(CALL)
        disabled = CaptureManager(settings(tmp_path, media_capture_enabled=False))
        with pytest.raises(CaptureRejected):
            disabled.reserve(CALL)
        assert list(tmp_path.iterdir()) == []
    asyncio.run(run())


def test_duplicate_socket_rejected_without_interrupting_original(tmp_path):
    async def run():
        manager = CaptureManager(settings(tmp_path))
        ticket = manager.reserve(CALL)
        first = Socket([start(ticket), media()])
        task = asyncio.create_task(manager.handle(first, CALL))
        while manager.active_count == 0:
            await asyncio.sleep(0)
        other = Socket([start(ticket), media()])
        await manager.handle(other, CALL)
        assert other.closed == 1008 and first.closed is None
        first.messages.put_nowait({"type": "websocket.receive", "text": json.dumps(stop())})
        await task
        assert read_manifest(tmp_path)["status"] == "completed"
    asyncio.run(run())


def test_queue_overflow_stays_bounded_and_flushes_prior_frames(tmp_path, monkeypatch):
    from media_capture import capture
    gate = threading.Event()
    original = capture._Recorder._write
    def delayed(recorder):
        gate.wait(timeout=1)
        original(recorder)
    monkeypatch.setattr(capture, "QUEUE_FRAMES", 1)
    monkeypatch.setattr(capture._Recorder, "_write", delayed)
    async def run():
        manager = CaptureManager(settings(tmp_path))
        ticket = manager.reserve(CALL)
        socket = Socket([start(ticket), media(), media(chunk=2, timestamp=20)])
        task = asyncio.create_task(manager.handle(socket, CALL))
        await asyncio.sleep(0.05)
        gate.set()
        await task
        manifest = read_manifest(tmp_path)
        assert manifest["status"] == "partial"
        assert manifest["finish_reason"] == "queue-overflow"
        assert manifest["counters"]["dropped_messages"] == 1
        assert manifest["tracks"]["inbound"]["frames"] == 1
    asyncio.run(run())


def test_symlink_storage_is_rejected_without_writing_outside(tmp_path):
    async def run():
        outside = tmp_path / "outside"
        outside.mkdir()
        link = tmp_path / "captures"
        link.symlink_to(outside, target_is_directory=True)
        manager = CaptureManager(settings(link))
        ticket = manager.reserve(CALL)
        socket = Socket([start(ticket), media()])
        await asyncio.wait_for(manager.handle(socket, CALL), timeout=2)
        assert list(outside.iterdir()) == []
        assert ticket.session.done.result()["status"] == "failed"
        assert socket.closed == 1000
    asyncio.run(run())


def test_low_disk_refuses_capture_without_call_actions(tmp_path, monkeypatch):
    monkeypatch.setattr("media_capture.capture.shutil.disk_usage",
                        lambda path: SimpleNamespace(free=1))
    async def run():
        manager = CaptureManager(settings(tmp_path))
        ticket = manager.reserve(CALL)
        socket = Socket([start(ticket), media()])
        await asyncio.wait_for(manager.handle(socket, CALL), timeout=2)
        assert not (tmp_path / CALL).exists()
        assert ticket.session.done.result()["finish_reason"] == "low-disk-space"
        assert socket.closed == 1000
    asyncio.run(run())


def test_signed_status_still_requires_registered_stream_name_and_sid(tmp_path):
    async def run():
        manager = CaptureManager(settings(tmp_path))
        ticket = manager.reserve(CALL)
        assert not manager.mark_status(CALL, STREAM, "stream-error", stream_name="other")
        assert manager.mark_status(CALL, STREAM, "stream-started", stream_name=ticket.stream_name)
        assert not manager.mark_status(CALL, "MZ" + "f" * 32, "stream-stopped", stream_name=ticket.stream_name)
        assert manager.mark_status(CALL, STREAM, "stream-error", stream_name=ticket.stream_name)
        await asyncio.gather(*list(manager.status_tasks))
        assert manager.pending_count == 0 and list(tmp_path.iterdir()) == []
        with pytest.raises(CaptureRejected):
            manager.reserve(CALL)
    asyncio.run(run())


def test_expired_tickets_and_registry_bound(tmp_path, monkeypatch):
    from media_capture import capture
    manager = CaptureManager(settings(tmp_path))
    ticket = manager.reserve(CALL)
    ticket.created -= 121
    assert manager.pending_count == 0
    with pytest.raises(CaptureRejected):
        manager.reserve(CALL)
    monkeypatch.setattr(capture, "MAX_TICKETS", 1)
    with pytest.raises(CaptureRejected):
        manager.reserve("CA" + "2" * 32)


def test_legitimate_long_silence_is_zero_padded_without_a_gap_limit(tmp_path):
    async def run():
        manager = CaptureManager(settings(tmp_path))
        ticket = manager.reserve(CALL)
        socket = Socket([start(ticket), media(timestamp=10_000), stop()])
        await manager.handle(socket, CALL)
        manifest = read_manifest(tmp_path)
        assert manifest["status"] == "completed"
        assert manifest["tracks"]["inbound"]["samples"] == 80_160
        assert manifest["tracks"]["inbound"]["gap_samples"] == 80_000
    asyncio.run(run())


def test_flush_timeout_is_bounded_and_does_not_claim_drained(tmp_path, monkeypatch):
    from media_capture import capture
    gate = threading.Event()
    original = capture._Recorder._write
    def delayed(recorder):
        gate.wait(timeout=1)
        original(recorder)
    monkeypatch.setattr(capture._Recorder, "_write", delayed)
    monkeypatch.setattr(capture, "FLUSH_TIMEOUT_SECONDS", 0.03)
    async def run():
        manager = CaptureManager(settings(tmp_path))
        ticket = manager.reserve(CALL)
        socket = Socket([start(ticket), media(), stop()])
        await asyncio.wait_for(manager.handle(socket, CALL), timeout=0.5)
        assert manager.active_count == 1
        assert ticket.session.finish_reason == "storage-timeout"
        gate.set()
        await asyncio.wrap_future(ticket.session.done)
        assert read_manifest(tmp_path)["status"] == "partial"
        assert manager.active_count == 0
    asyncio.run(run())
