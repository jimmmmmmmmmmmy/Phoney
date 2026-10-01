"""Exercise actual writer scheduling with a deterministic monotonic clock."""

import asyncio
import base64
from types import SimpleNamespace

from operator_service import audio


class Clock:
    def __init__(self):
        self.now = 0.0
        self.on_wait = None

    def monotonic(self):
        return self.now

    async def wait(self, seconds=None):
        if self.on_wait is not None and self.on_wait(seconds):
            return
        if seconds is None:
            raise asyncio.CancelledError
        self.now += max(0, seconds)
        await asyncio.sleep(0)


class Socket:
    def __init__(self, clock):
        self.clock = clock
        self.sent = []
        self.delay_frame = None
        self.on_send = None

    async def send_json(self, message):
        if message["event"] == "media" and self.delay_frame == len(self.media):
            self.clock.now += .08
        self.sent.append((self.clock.now, message))
        if self.on_send is not None:
            self.on_send(message)

    @property
    def media(self):
        return [(stamp, base64.b64decode(message["media"]["payload"]))
                for stamp, message in self.sent if message["event"] == "media"]


def channel(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=clock.monotonic))
    writer = audio.OutputChannel("remote")
    socket = Socket(clock)
    writer._socket = socket
    writer.stream_sid = "MZ" + "1" * 32
    writer._wait = clock.wait
    diagnostics = []
    writer.on_diagnostic = diagnostics.append
    return clock, writer, socket, diagnostics


def drain(writer):
    async def run():
        try:
            await writer._write()
        except asyncio.CancelledError:
            pass
    asyncio.run(run())


def test_writer_lifecycle_keeps_cadence_through_marks_clear_provider_pause_and_socket_stall(monkeypatch):
    clock, writer, socket, diagnostics = channel(monkeypatch)
    writer.clear()
    for _ in range(6):
        writer.send(b"\x2a" * 160, kind="agent", reply_epoch=4)
    writer.mark("reply-4-1")
    resumed = False
    replaced = False
    ready_to_replace = False

    def next_phrase(message):
        nonlocal ready_to_replace
        if message["event"] != "mark":
            return
        if message["mark"]["name"] == "reply-4-1":
            for _ in range(6):
                writer.send(b"\x2b" * 160, kind="agent", reply_epoch=5)
        elif message["mark"]["name"] == "reply-5-1":
            # A new slow source enters its startup reserve. A cancellation
            # interrupts that wait before any stale speech reaches Twilio.
            writer.send(b"\x2c" * 160, kind="agent", reply_epoch=6)
            ready_to_replace = True

    def replenish(seconds):
        nonlocal resumed, replaced
        if seconds is None and not resumed:
            resumed = True
            clock.now += .08
            for _ in range(3):
                writer.send(b"\x2b" * 160, kind="agent", reply_epoch=5)
            writer.mark("reply-5-1")
            return True
        if seconds is not None and ready_to_replace and not replaced:
            replaced = True
            clock.now += .005
            writer.mark("stale-reply")
            dropped, invalidated = writer.clear()
            assert dropped == 1 and invalidated == ["stale-reply"]
            for _ in range(8):
                writer.send(b"\x2d" * 160, kind="agent", reply_epoch=7)
            writer.mark("reply-7-1")
            socket.delay_frame = len(socket.media) + 2
            return True
        return False

    socket.on_send = next_phrase
    clock.on_wait = replenish
    drain(writer)
    times = [round(t, 3) for t, _ in socket.media]
    assert times[:6] == [0, .02, .04, .06, .08, .10]
    assert times[6:12] == [.12, .14, .16, .18, .20, .22]
    assert times[12:15] == [.32, .34, .36]
    assert times[15:20] == [.365, .385, .485, .505, .525]
    assert len(socket.media) == 23
    assert [payload[0] for _, payload in socket.media] == [0x2a] * 6 + [0x2b] * 9 + [0x2d] * 8
    assert all(payload != b"\xff" * 160 for _, payload in socket.media)
    marks = [(t, message["mark"]["name"]) for t, message in socket.sent if message["event"] == "mark"]
    assert [name for _, name in marks] == ["reply-4-1", "reply-5-1", "reply-7-1"]
    assert marks[0][0] == socket.media[5][0]
    assert socket.sent[0][1]["event"] == "clear" and socket.sent[0][0] == 0
    assert [round(t, 3) for t, message in socket.sent if message["event"] == "clear"] == [0, .365]
    assert writer.counters["underruns"] == 1
    assert len([d for d in diagnostics if d["event"] == "output-underrun"]) == 1
    assert any(d["event"] == "output-send-delayed" and d["interval_ms"] == 100 for d in diagnostics)
    stats = [d for d in diagnostics if d["event"] == "output-speech-stats"]
    assert [d["frames"] for d in stats] == [6, 9, 8]
    assert [d["reply_epoch"] for d in stats] == [4, 5, 7]
    assert all(d["source_zero_frames"] == 0 for d in stats)


def test_reserve_keeps_short_phrases_live_audio_and_cached_announcements_bounded(monkeypatch):
    for kind, marked, expected_start in [("agent", False, .12), ("announcement", True, 0), ("live", False, 0)]:
        _, writer, socket, _ = channel(monkeypatch)
        writer.send(b"\x2a" * 160, kind=kind)
        if marked:
            writer.mark("short-phrase")
        drain(writer)
        assert round(socket.media[0][0], 3) == expected_start
        assert len(socket.media) == 1
    _, writer, socket, _ = channel(monkeypatch)
    for index in range(audio.AGENT_QUEUE_FRAMES):
        writer.send(bytes([index + 1]) * 160, kind="announcement")
    writer.mark("announcement-done")
    drain(writer)
    assert [payload[0] for _, payload in socket.media] == list(range(1, audio.AGENT_QUEUE_FRAMES + 1))
    assert writer.counters.get("dropped", 0) == 0
