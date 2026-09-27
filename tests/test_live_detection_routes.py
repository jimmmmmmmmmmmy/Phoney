"""Live Twilio media tap checks using local provider and telephony fakes."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import create_app
from media_capture.capture import decode_mulaw
from test_media_webhooks import (
    Gateway, OTHER, PARENT, SETTINGS, assert_socket_closed, send_audio, send_start,
    socket_headers, stop_message, stream_and_token,
)
from test_transcription import until


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


class HoldingDetectionSocket(DetectionSocket):
    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def messages(self):
        await self.ended.wait()
        await self.release.wait()
        async for message in super().messages():
            yield message


@contextmanager
def detection_client(tmp_path):
    settings = replace(
        SETTINGS,
        media_capture_enabled=True,
        media_storage_dir=str(tmp_path / "captures"),
        modulate_detection_enabled=True,
        modulate_api_key="fixture-modulate-key",
        detection_storage_dir=str(tmp_path / "detection"),
    )
    gateway = Gateway()
    connector = DetectionConnector()
    app = create_app(settings, gateway=gateway, detection_connector=connector)
    with TestClient(app, base_url=settings.public_base_url) as client:
        yield client, settings, gateway, connector


def test_live_detection_tap_sends_only_inbound_pcm_without_affecting_call(tmp_path):
    with detection_client(tmp_path) as (client, settings, gateway, connector):
        _, token = stream_and_token(client, settings)
        with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)) as socket:
            send_start(socket, settings, token)
            send_audio(socket, track="inbound", sample=0x00)
            send_audio(socket, track="outbound", sample=0x80)
            socket.send_json(stop_message(settings))
            assert assert_socket_closed(socket) == 1000

        client.portal.call(until, lambda: client.app.state.live_detection.active_count == 0)

        assert len(connector.sockets) == 1
        sent_audio = [item for item in connector.sockets[0].sent if isinstance(item, bytes)]
        assert sent_audio == [decode_mulaw(b"\x00" * 160)]
        assert connector.sockets[0].sent[-1] == ""
        assert client.app.state.switchboard.active_count == 1
        assert gateway.ended_calls == []
        assert gateway.ended_conferences == []
        client.portal.call(until, lambda: not client.app.state.detection_writes)
        saved = client.app.state.detection_store.get(PARENT)
        assert saved["label"] == "unknown"  # A 20ms fixture cannot establish a verdict.
        assert saved["status"] == "unknown"
        public = client.get("/api/transcripts").text
        assert "fixture-modulate-key" not in public


def test_deploy_drain_counts_provider_flush_after_media_stream_stops(tmp_path):
    settings = replace(
        SETTINGS,
        media_capture_enabled=True,
        media_storage_dir=str(tmp_path / "captures"),
        modulate_detection_enabled=True,
        modulate_api_key="fixture-modulate-key",
        detection_storage_dir=str(tmp_path / "detection"),
    )
    provider = HoldingDetectionSocket()
    app = create_app(settings, gateway=Gateway(), detection_connector=lambda url: provider)
    headers = {"Authorization": "Bearer " + settings.deploy_control_token}

    with TestClient(app, base_url=settings.public_base_url) as client:
        _, token = stream_and_token(client, settings)
        with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)) as socket:
            send_start(socket, settings, token)
            send_audio(socket, sample=0x00)
            socket.send_json(stop_message(settings))
            assert assert_socket_closed(socket) == 1000

        client.portal.call(until, lambda: client.app.state.live_detection.active_count == 1)
        client.portal.call(until, lambda: client.app.state.media_capture.active_count == 0
                           and not client.app.state.detection_writes)
        assert client.get("/internal/deploy", headers=headers).json()["pending_work"] == 1
        client.portal.call(provider.release.set)
        client.portal.call(until, lambda: client.app.state.live_detection.active_count == 0
                           and not client.app.state.detection_writes)
        assert client.get("/internal/deploy", headers=headers).json()["pending_work"] == 0


@pytest.mark.parametrize("rejection", ["unsigned", "bad-signature", "unknown-call", "bad-token"])
def test_invalid_twilio_stream_never_reaches_modulate(tmp_path, rejection):
    with detection_client(tmp_path) as (client, settings, gateway, connector):
        _, token = stream_and_token(client, settings)
        path = f"/media/{OTHER if rejection == 'unknown-call' else PARENT}/"
        headers = socket_headers(settings, path=path)
        if rejection == "unsigned":
            headers = {}
        elif rejection == "bad-signature":
            headers = {"X-Twilio-Signature": "invalid"}
        if rejection == "bad-token":
            with client.websocket_connect(path, headers=headers) as socket:
                send_start(socket, settings, "bad-token")
                assert assert_socket_closed(socket) == 1008
        else:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(path, headers=headers):
                    pytest.fail("Unauthorized stream accepted")
        assert connector.urls == []
        assert gateway.ended_calls == []


@pytest.mark.parametrize("mode", ["disabled", "candidate", "draining"])
def test_disabled_candidate_or_draining_process_never_opens_detector(tmp_path, mode):
    settings = replace(SETTINGS, media_capture_enabled=True,
        media_storage_dir=str(tmp_path / "audio"),
        modulate_detection_enabled=mode != "disabled", modulate_api_key="private-fixture-key",
        detection_storage_dir=str(tmp_path / "detection"),
        deploy_commit="a" * 40 if mode == "candidate" else "",
        deploy_trigger_path=str(tmp_path / "deploy.trigger") if mode == "candidate" else "")
    connector = DetectionConnector()
    app = create_app(settings, gateway=Gateway(), detection_connector=connector)
    with TestClient(app, base_url=settings.public_base_url) as client:
        if mode == "draining":
            client.portal.call(app.state.switchboard.set_draining, True)
        client.portal.call(app.state.live_detection.start, PARENT, "MZ" + "4" * 32)
        client.portal.call(app.state.live_detection.offer, PARENT, "inbound", 0, b"\x00" * 160)
        client.portal.call(app.state.live_detection.finish, PARENT)
        client.portal.call(app.state.live_detection.wait_idle)
        assert connector.urls == []
        assert not app.state.detection_writes


def test_observer_failure_is_isolated_for_each_consumer():
    from media_capture.capture import _observe
    calls = []

    class Broken:
        def offer(self, *args):
            raise RuntimeError("test failure")

    class Healthy:
        def offer(self, *args):
            calls.append(args)

    _observe((Broken(), Healthy(), None), "offer", PARENT, "inbound", 0, b"audio")
    assert len(calls) == 1


def test_pending_disk_write_counts_for_drain_and_shutdown_budget(tmp_path, monkeypatch):
    import threading
    import time

    settings = replace(SETTINGS, media_capture_enabled=True,
        media_storage_dir=str(tmp_path / "audio"), modulate_detection_enabled=True,
        modulate_api_key="private-fixture-key", detection_storage_dir=str(tmp_path / "detection"))
    app = create_app(settings, gateway=Gateway(), detection_connector=DetectionConnector())
    entered, release = threading.Event(), threading.Event()

    def stalled_save(*args):
        entered.set()
        release.wait(3)
        return True

    monkeypatch.setattr(app.state.detection_store, "save", stalled_save)
    monkeypatch.setattr("app.DETECTION_SAVE_SHUTDOWN_SECONDS", 0.02)
    app.state.live_detection._shutdown_seconds = 0.02

    async def scenario():
        try:
            async with app.router.lifespan_context(app):
                app.state.live_detection.start(PARENT, "MZ" + "4" * 32)
                app.state.live_detection.offer(PARENT, "inbound", 0, b"\x00" * 160)
                app.state.live_detection.finish(PARENT)
                await until(entered.is_set)
                await app.state.live_detection.wait_idle()
                assert app.state.live_detection.active_count == 0
                assert app.state.detection_writes
                # The route counts these writes even after provider finalization.
                import httpx
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url=settings.public_base_url) as client:
                    response = await client.get("/internal/deploy", headers={
                        "Authorization": "Bearer " + settings.deploy_control_token})
                    assert response.json()["pending_work"] >= 1
                started = time.monotonic()
            assert time.monotonic() - started < 0.5
            assert not app.state.detection_writes
        finally:
            release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider_fails", [False, True])
def test_detection_persists_while_transcription_capture_and_conference_continue(tmp_path, provider_fails):
    from test_transcription import Connector, result
    from test_media_webhooks import read_manifest

    settings = replace(SETTINGS, media_capture_enabled=True,
        media_storage_dir=str(tmp_path / "audio"), modulate_detection_enabled=True,
        modulate_api_key="private-fixture-key", detection_storage_dir=str(tmp_path / "detection"),
        transcription_enabled=True, deepgram_api_key="private-deepgram-key",
        transcript_storage_dir=str(tmp_path / "transcripts"))
    transcription = Connector()
    detector = DetectionConnector()

    def connect(url):
        if provider_fails:
            raise RuntimeError("provider connection failed")
        return detector(url)

    gateway = Gateway()
    app = create_app(settings, gateway=gateway, transcription_connector=transcription,
                     detection_connector=connect)
    with TestClient(app, base_url=settings.public_base_url) as client:
        _, token = stream_and_token(client, settings)
        with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)) as socket:
            send_start(socket, settings, token)
            client.portal.call(until, lambda: len(transcription.sockets) == 2)
            for n in range(200):
                send_audio(socket, timestamp=n * 20, chunk=n + 1, sample=0x00)
            send_audio(socket, track="outbound", sample=0x80)
            client.portal.call(until, lambda: sum(len(v) for v in transcription.sockets[0].sent
                                                if isinstance(v, bytes)) == 32000)
            client.portal.call(transcription.sockets[0].push, result("Fixture caller speech."))
            client.portal.call(until, lambda: bool(app.state.transcription.snapshot()["sessions"][0]["segments"]))
            socket.send_json(stop_message(settings))
            assert assert_socket_closed(socket) == 1000
        client.portal.call(until, lambda: app.state.live_detection.active_count == 0
            and app.state.media_capture.active_count == 0 and not app.state.detection_writes)
        saved = app.state.detection_store.get(PARENT)
        assert saved["label"] == ("unknown" if provider_fails else "non-synthetic")
        assert saved["status"] == ("unknown" if provider_fails else "complete")
        assert read_manifest(settings)["tracks"]["inbound"]["frames"] == 200
        snapshot = client.get("/api/transcripts").json()
        assert snapshot["sessions"][0]["segments"][0]["text"] == "Fixture caller speech."
        assert snapshot["sessions"][0]["detection"]["label"] == saved["label"]
        assert app.state.switchboard.active_count == 1
        assert gateway.ended_calls == [] and gateway.ended_conferences == []
