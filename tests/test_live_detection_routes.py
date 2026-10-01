"""Focused product and boundary checks; test helpers live in support."""

from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from app import create_app

from support.live_detection_routes import DetectionConnector
from support.media_webhooks import Gateway, PARENT, SETTINGS, assert_socket_closed, send_audio, send_start, socket_headers, stop_message, stream_and_token
from support.transcription import until


@pytest.mark.parametrize("provider_fails", [False, True])
def test_detection_persists_while_transcription_capture_and_conference_continue(tmp_path, provider_fails):
    from support.transcription import Connector, result
    from support.media_webhooks import read_manifest

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
