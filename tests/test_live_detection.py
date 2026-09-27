"""Network-free checks for the bounded live Modulate worker."""

import asyncio
import json
from types import SimpleNamespace

from integrations.contracts import AudioFrame
from partner_detection import LiveDetectionManager, LiveDetectionWorker


def frame(timestamp=0, track="inbound"):
    return AudioFrame("CA-live", "MZ-live", track, timestamp, b"\x01\x00" * 160)


def test_live_worker_offer_never_blocks_when_the_queue_is_full():
    worker = LiveDetectionWorker(api_key="private-test-key", queue_frames=1)

    assert worker.offer(frame()) is True
    assert worker.offer(frame(timestamp=20)) is False
    assert worker.queued_frames == 1
    assert worker.dropped_frames == 1


class ProviderSocket:
    def __init__(self):
        self.sent = []
        self.ended = asyncio.Event()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def send(self, message):
        self.sent.append(message)
        if message == "":
            self.ended.set()

    def __aiter__(self):
        return self.messages()

    async def messages(self):
        await self.ended.wait()
        yield json.dumps({"type": "frame", "frame": {
            "start_time_ms": 0, "end_time_ms": 20,
            "verdict": "non-synthetic", "confidence": 0.94,
        }})
        yield json.dumps({"type": "done", "duration_ms": 20, "frame_count": 1})


def test_live_worker_streams_queued_audio_and_returns_an_outcome():
    async def scenario():
        socket = ProviderSocket()
        worker = LiveDetectionWorker(api_key="private-test-key", queue_frames=2,
                                     connector=lambda url: socket)
        task = asyncio.create_task(worker.run())
        assert worker.offer(frame()) is True
        worker.finish()
        outcome = await task
        return socket, outcome

    socket, outcome = asyncio.run(scenario())

    assert socket.sent == [b"\x01\x00" * 160, ""]
    assert socket.closed is True
    assert outcome.report.status == "non-synthetic"
    assert outcome.accepted_frames == 1
    assert outcome.dropped_frames == 0


def test_live_detection_manager_reconnects_with_a_fresh_stream():
    async def scenario():
        sockets = []

        def connector(url):
            socket = ProviderSocket()
            sockets.append(socket)
            return socket

        settings = SimpleNamespace(
            modulate_detection_enabled=True,
            modulate_api_key="private-test-key",
            modulate_detection_queue_frames=2,
            modulate_detection_deadline_seconds=5,
            modulate_detection_max_audio_seconds=10,
            modulate_detection_min_confidence=0.80,
        )
        manager = LiveDetectionManager(settings, connector=connector)

        manager.start("CA-live", "MZ-first")
        manager.offer("CA-live", "inbound", 0, b"\x00" * 160)
        manager.finish("CA-live", "socket-disconnected")
        await manager.wait_idle()

        manager.start("CA-live", "MZ-second")
        manager.offer("CA-live", "inbound", 0, b"\x00" * 160)
        manager.finish("CA-live", "stream-stopped")
        await manager.wait_idle()
        return manager, sockets

    manager, sockets = asyncio.run(scenario())

    assert len(sockets) == 2
    assert [socket.sent[-1] for socket in sockets] == ["", ""]
    assert manager.active_count == 0
    assert len(manager.completed) == 2
    assert manager.decisions["CA-live"].label == "unknown"
    assert manager.decisions["CA-live"].reason == "provider_incomplete"
    assert manager.decisions["CA-live"].streams == 2


def test_live_detection_manager_shutdown_cancels_a_stalled_provider():
    class StalledSocket(ProviderSocket):
        async def messages(self):
            await asyncio.Event().wait()
            if False:
                yield ""

    async def scenario():
        settings = SimpleNamespace(
            modulate_detection_enabled=True,
            modulate_api_key="private-test-key",
            modulate_detection_queue_frames=2,
            modulate_detection_deadline_seconds=120,
            modulate_detection_max_audio_seconds=10,
        )
        manager = LiveDetectionManager(
            settings, connector=lambda url: StalledSocket(), shutdown_seconds=0.01)
        manager.start("CA-live", "MZ-live")
        manager.offer("CA-live", "inbound", 0, b"\x00" * 160)
        await asyncio.wait_for(manager.close(), timeout=0.2)
        return manager

    manager = asyncio.run(scenario())

    assert manager.closed is True
    assert manager.active_count == 0


def settings(**overrides):
    return SimpleNamespace(**({
        "modulate_detection_enabled": True,
        "modulate_api_key": "private-test-key",
        "modulate_detection_queue_frames": 8,
        "modulate_detection_deadline_seconds": .2,
        "modulate_detection_max_audio_seconds": 10,
        "modulate_detection_min_confidence": .80,
    } | overrides))


class SizedProviderSocket(ProviderSocket):
    async def messages(self):
        await self.ended.wait()
        duration = sum(len(item) for item in self.sent if isinstance(item, bytes)) // 16
        yield json.dumps({"type": "frame", "frame": {
            "start_time_ms": 0, "end_time_ms": duration,
            "verdict": "non-synthetic", "confidence": .94}})
        yield json.dumps({"type": "done", "duration_ms": duration, "frame_count": 1})


def test_candidate_gate_prevents_provider_connection_and_state_writes():
    updates, connections = [], []
    async def scenario():
        manager = LiveDetectionManager(settings(), can_run=lambda: False,
                    connector=lambda url: connections.append(url),
                    on_update=lambda sid, payload: updates.append(payload))
        manager.start("CA-gated", "MZ-gated")
        await manager.wait_idle()
        return manager
    manager = asyncio.run(scenario())
    assert manager.active_count == 0
    assert not updates and not connections


def test_live_manager_publishes_analyzing_then_safe_final_result():
    updates = []
    async def scenario():
        manager = LiveDetectionManager(settings(), connector=lambda url: SizedProviderSocket(),
                    on_update=lambda sid, payload: updates.append((sid, payload)))
        manager.start("CA-result", "MZ-result")
        manager.offer("CA-result", "inbound", 120, b"\0" * 32000)
        manager.finish("CA-result")
        await manager.wait_idle()
        return manager
    manager = asyncio.run(scenario())
    assert manager.active_count == 0
    assert [item[1]["status"] for item in updates] == ["analyzing", "complete"]
    assert [item[1]["streams"] for item in updates] == [1, 1]
    result = updates[-1][1]
    assert result["label"] == "non-synthetic"
    assert result["confidence"] == .94
    assert result["submitted_audio_ms"] == 4000
    assert result["coverage_limited"] is False
    assert set(result) == {"provider", "status", "label", "confidence", "reason", "streams",
                           "observations", "accepted_frames", "dropped_frames", "submitted_audio_ms",
                           "coverage_limited", "analysis"}
    assert "private-test-key" not in json.dumps(result)


def test_cancelled_provider_leaves_unknown_instead_of_analyzing_forever():
    updates = []
    class StalledSocket(ProviderSocket):
        async def messages(self):
            await asyncio.Event().wait()
            if False:
                yield ""
    async def scenario():
        manager = LiveDetectionManager(settings(), shutdown_seconds=.01,
                    connector=lambda url: StalledSocket(),
                    on_update=lambda sid, payload: updates.append(payload))
        manager.start("CA-cancel", "MZ-cancel")
        manager.offer("CA-cancel", "inbound", 0, b"\0" * 160)
        await manager.close()
        return manager
    manager = asyncio.run(scenario())
    assert manager.active_count == 0
    assert updates[-1]["status"] == "unknown"
    assert updates[-1]["reason"] == "cancelled"


def test_worker_stops_accepting_after_audio_cap_while_provider_finalizes():
    class DelayedResult(SizedProviderSocket):
        async def messages(self):
            await self.ended.wait()
            await asyncio.sleep(.03)
            async for message in super().messages():
                yield message
    async def scenario():
        socket = DelayedResult()
        worker = LiveDetectionWorker(api_key="key", queue_frames=1, max_audio_seconds=1,
                                     deadline_seconds=.2, connector=lambda url: socket)
        task = asyncio.create_task(worker.run())
        worker.offer(frame_with_samples(8000))
        await socket.ended.wait()
        for _ in range(20):
            assert worker.offer(frame()) is False
        outcome = await task
        return worker, outcome
    worker, outcome = asyncio.run(scenario())
    assert outcome.report.coverage_limited is True
    assert outcome.dropped_frames == 0
    assert worker.queued_frames == 0


def frame_with_samples(count, timestamp=0):
    return AudioFrame("CA-live", "MZ-live", "inbound", timestamp, b"\1\0" * count)


def test_history_and_call_reconnect_attempts_are_bounded():
    updates = []
    async def scenario():
        manager = LiveDetectionManager(settings(), connector=lambda url: SizedProviderSocket(),
                    on_update=lambda sid, payload: updates.append(payload))
        manager.MAX_HISTORY_CALLS = 2
        manager.MAX_STREAMS_PER_CALL = 2
        for index in range(5):
            sid = f"CA-{index}"
            manager.start(sid, f"MZ-{index}")
            manager.offer(sid, "inbound", 0, b"\0" * 160)
            manager.finish(sid)
            await manager.wait_idle()
        for index in range(2):
            manager.start("CA-last", f"MZ-last-{index}")
            manager.offer("CA-last", "inbound", 0, b"\0" * 160)
            manager.finish("CA-last")
            await manager.wait_idle()
        manager.start("CA-last", "MZ-last-extra")
        return manager
    manager = asyncio.run(scenario())
    assert len(manager._completed_by_call) == 2
    assert len(manager.decisions) == 2
    assert len(manager._stream_counts) == 2
    assert updates[-1]["reason"] == "too_many_streams"
    assert updates[-1]["status"] == "unknown"
    assert manager.active_count == 0


def test_reconnect_does_not_publish_old_final_result_over_new_analyzing_state():
    updates = []
    class DelayedResult(SizedProviderSocket):
        async def messages(self):
            await self.ended.wait()
            await asyncio.sleep(.04)
            async for message in super().messages():
                yield message
    async def scenario():
        sockets = [DelayedResult(), SizedProviderSocket()]
        manager = LiveDetectionManager(settings(), connector=lambda url: sockets.pop(0),
                    on_update=lambda sid, payload: updates.append(payload))
        manager.start("CA-overlap", "MZ-first")
        manager.offer("CA-overlap", "inbound", 0, b"\0" * 160)
        await asyncio.sleep(0)
        manager.start("CA-overlap", "MZ-second")
        manager.offer("CA-overlap", "inbound", 0, b"\0" * 160)
        manager.finish("CA-overlap")
        await manager.wait_idle()
    asyncio.run(scenario())
    assert [item["status"] for item in updates] == ["analyzing", "analyzing", "unknown"]
    assert [item["streams"] for item in updates] == [1, 2, 2]


def test_call_budget_is_shared_by_reconnected_streams_and_clips_final_frame():
    sockets, updates = [], []
    def connector(url):
        socket = SizedProviderSocket()
        sockets.append(socket)
        return socket
    async def scenario():
        manager = LiveDetectionManager(settings(modulate_detection_max_audio_seconds=5),
                    connector=connector, on_update=lambda sid, payload: updates.append(payload))
        manager.start("CA-budget", "MZ-first")
        manager.offer("CA-budget", "inbound", 100, b"\0" * 28000)  # 3.5 seconds
        manager.finish("CA-budget")
        await manager.wait_idle()
        manager.start("CA-budget", "MZ-second")
        manager.offer("CA-budget", "inbound", 500, b"\0" * 28000)  # only 1.5 seconds remain
        await manager.wait_idle()
        manager.start("CA-budget", "MZ-third")
        return manager
    manager = asyncio.run(scenario())
    assert len(sockets) == 2
    assert sum(len(data) for socket in sockets for data in socket.sent if isinstance(data, bytes)) == 5 * 16000
    assert updates[-1]["status"] == "unknown"  # Unmapped reconnect epochs remain conservative.
    assert updates[-1]["analysis"]["alert"] == "inconclusive"
    assert updates[-1]["coverage_limited"] is True
    assert updates[-1]["submitted_audio_ms"] == 5000
    assert manager.active_count == 0


def test_finishing_a_full_queue_retains_every_accepted_frame():
    async def scenario():
        socket = SizedProviderSocket()
        worker = LiveDetectionWorker(api_key="key", queue_frames=1, connector=lambda url: socket)
        assert worker.offer(frame_with_samples(32000))
        worker.finish()
        outcome = await worker.run()
        return socket, outcome
    socket, outcome = asyncio.run(scenario())
    assert len(socket.sent[0]) == 64000
    assert outcome.report.submitted_audio_ms == 4000
    assert outcome.dropped_frames == 0


def test_history_eviction_preserves_older_streams_still_finalizing():
    class HeldResult(SizedProviderSocket):
        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()
        async def messages(self):
            await self.release.wait()
            async for message in super().messages():
                yield message
    async def scenario():
        first = HeldResult()
        sockets = [first, SizedProviderSocket(), SizedProviderSocket()]
        manager = LiveDetectionManager(settings(), connector=lambda url: sockets.pop(0))
        manager.MAX_HISTORY_CALLS = 1
        manager.start("CA-overlap", "MZ-old")
        manager.offer("CA-overlap", "inbound", 0, b"\0" * 160)
        await asyncio.sleep(0)
        manager.start("CA-overlap", "MZ-new")
        manager.offer("CA-overlap", "inbound", 0, b"\0" * 160)
        latest = manager._sessions["CA-overlap"].task
        manager.finish("CA-overlap")
        await latest
        await asyncio.sleep(0)
        assert "CA-overlap" not in manager._sessions
        assert "CA-overlap" in manager._pending_by_call
        manager.start("CA-other", "MZ-other")
        manager.offer("CA-other", "inbound", 0, b"\0" * 160)
        other = manager._sessions["CA-other"].task
        manager.finish("CA-other")
        await other
        await asyncio.sleep(0)
        assert "CA-overlap" in manager._completed_by_call
        first.release.set()
        await manager.wait_idle()
        return manager
    manager = asyncio.run(scenario())
    assert manager.decisions["CA-overlap"].streams == 2
    assert manager.decisions["CA-overlap"].label == "unknown"
    assert len(manager._completed_by_call) == 1


def test_live_windows_publish_throttled_alert_before_call_finishes(monkeypatch):
    from partner_detection import live
    clock = [100.0]
    monkeypatch.setattr(live.time, "monotonic", lambda: clock[0])
    updates = []
    class LiveSocket(ProviderSocket):
        def __init__(self):
            super().__init__()
            self.audio_ready = asyncio.Event()
            self.observed = asyncio.Event()
        async def send(self, message):
            await super().send(message)
            if isinstance(message, bytes):
                self.audio_ready.set()
        async def messages(self):
            await self.audio_ready.wait()
            for start, end in ((0, 4000), (4000, 8000)):
                yield json.dumps({"type": "frame", "frame": {
                    "start_time_ms": start, "end_time_ms": end,
                    "verdict": "synthetic", "confidence": .95}})
            self.observed.set()
            await self.ended.wait()
            yield json.dumps({"type": "done", "duration_ms": 8000, "frame_count": 2})
    async def scenario():
        socket = LiveSocket()
        manager = LiveDetectionManager(settings(), connector=lambda url: socket,
                    on_update=lambda sid, payload: updates.append(payload))
        manager.start("CA-progress", "MZ-progress")
        clock[0] += 3
        manager.offer("CA-progress", "inbound", 100, b"\0" * 64000)
        await socket.observed.wait()
        assert manager.active_call_ids == {"CA-progress"}
        assert len(updates) == 2  # Initial state plus one throttled progress publication.
        progress = updates[-1]
        assert progress["status"] == "analyzing"
        assert progress["analysis"]["alert"] == "ai_caller"
        assert progress["analysis"]["complete"] is False
        assert progress["analysis"]["windows"][0]["start_ms"] == 100
        manager.finish("CA-progress")
        await manager.wait_idle()
        assert not manager.active_call_ids
    asyncio.run(scenario())
    assert len(updates) == 3
    assert updates[-1]["status"] == "complete"
    assert updates[-1]["analysis"]["complete"] is True
    assert updates[-1]["analysis"]["synthetic_ms"] == 8000


def test_transport_failure_retains_partial_windows_but_invalid_response_discards_them():
    async def scenario(invalid):
        updates = []
        class FailingSocket(ProviderSocket):
            async def messages(self):
                await self.ended.wait()
                yield json.dumps({"type": "frame", "frame": {
                    "start_time_ms": 0, "end_time_ms": 4000,
                    "verdict": "synthetic", "confidence": .95}})
                yield json.dumps({"type": "unexpected"} if invalid else {"type": "error"})
        manager = LiveDetectionManager(settings(), connector=lambda url: FailingSocket(),
                    on_update=lambda sid, payload: updates.append(payload))
        manager.start("CA-failed", "MZ-failed")
        manager.offer("CA-failed", "inbound", 0, b"\0" * 32000)
        manager.finish("CA-failed")
        await manager.wait_idle()
        return updates[-1]
    transport = asyncio.run(scenario(False))
    assert transport["status"] == "unknown"
    assert transport["analysis"]["complete"] is False
    assert transport["analysis"]["alert"] == "ai_caller"
    assert len(transport["analysis"]["windows"]) == 1
    invalid = asyncio.run(scenario(True))
    assert invalid["status"] == "unknown"
    assert invalid["analysis"]["alert"] == "inconclusive"
    assert invalid["analysis"]["windows"] == []
