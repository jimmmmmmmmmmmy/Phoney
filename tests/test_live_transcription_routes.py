"""Signed Twilio-to-viewer integration; fake telephony and provider sockets only."""

from contextlib import contextmanager
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import create_app
from test_media_webhooks import (
    Gateway, OTHER, PARENT, SETTINGS, assert_socket_closed, read_manifest,
    send_audio, send_start, signed_post, socket_headers, stop_message, stream_and_token,
)
from test_transcription import Connector, result, until


@contextmanager
def live_client(tmp_path, *, options=None):
    settings = replace(SETTINGS, media_capture_enabled=True,
                       media_storage_dir=str(tmp_path / "captures"),
                       transcription_enabled=True, deepgram_api_key="fixture-private-key",
                       transcript_storage_dir=str(tmp_path / "transcripts"),
                       dashboard_token="integration-viewer-access-code-32-characters")
    gateway, connector = Gateway(), Connector(options)
    app = create_app(settings, gateway=gateway, transcription_connector=connector)
    with TestClient(app, base_url=settings.public_base_url) as client:
        yield client, settings, gateway, connector


def viewer(client, settings):
    response = client.post("/dashboard/login", json={"token": settings.dashboard_token},
                           headers={"Origin": settings.public_base_url})
    assert response.status_code == 200


def wait_for(client, predicate):
    client.portal.call(until, predicate)


def final_session(client):
    return client.app.state.transcription.snapshot()["sessions"][0]


def finish_call(client, settings):
    response = signed_post(client, settings, f"/conference/finished/{PARENT}",
                           {"AccountSid": settings.account_sid, "CallSid": PARENT})
    assert response.status_code == 200
    client.portal.call(client.app.state.switchboard.wait_idle)


@pytest.mark.parametrize("rejection", ["unsigned", "wrong-signature", "unknown-call", "bad-start-token"])
def test_rejected_twilio_stream_never_opens_provider(tmp_path, rejection):
    with live_client(tmp_path) as (client, settings, gateway, connector):
        _, token = stream_and_token(client, settings)
        path = f"/media/{OTHER if rejection == 'unknown-call' else PARENT}/"
        headers = socket_headers(settings, path=path)
        if rejection == "unsigned":
            headers = {}
        elif rejection == "wrong-signature":
            headers = {"X-Twilio-Signature": "wrong"}
        if rejection == "bad-start-token":
            with client.websocket_connect(path, headers=headers) as socket:
                send_start(socket, settings, "invalid-" + token)
                assert assert_socket_closed(socket) == 1008
        else:
            with pytest.raises(WebSocketDisconnect) as rejected:
                with client.websocket_connect(path, headers=headers):
                    pytest.fail("Unauthorized stream was accepted")
            assert rejected.value.code == 1008
        assert connector.requests == []
        assert client.app.state.transcription.snapshot()["sessions"] == []
        assert not (tmp_path / "transcripts").exists()
        assert gateway.ended_calls == []


def test_signed_two_track_audio_reaches_private_live_view_and_final_exports(tmp_path):
    with live_client(tmp_path) as (client, settings, gateway, connector):
        _, token = stream_and_token(client, settings)
        assert client.get("/api/transcripts").status_code == 401
        viewer(client, settings)
        with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)) as socket:
            send_start(socket, settings, token)
            send_audio(socket, track="inbound", sample=0x00)
            send_audio(socket, track="outbound", sample=0x80)
            wait_for(client, lambda: len(connector.sockets) == 2 and all(
                any(isinstance(value, bytes) for value in provider.sent)
                for provider in connector.sockets))
            assert b"".join(value for value in connector.sockets[0].sent
                            if isinstance(value, bytes)) == b"\x00" * 160
            assert b"".join(value for value in connector.sockets[1].sent
                            if isinstance(value, bytes)) == b"\x80" * 160
            client.portal.call(connector.sockets[0].push, result("Still speaking", final=False))
            wait_for(client, lambda: final_session(client)["tracks"]["inbound"]["interim"] == "Still speaking")
            live = client.get("/api/transcripts").json()
            assert live["selected_call_sid"] == PARENT
            assert live["sessions"][0]["tracks"]["inbound"]["interim"] == "Still speaking"
            client.portal.call(connector.sockets[0].push, result("Caller words."))
            client.portal.call(connector.sockets[1].push, result("Playback words."))
            wait_for(client, lambda: len(final_session(client)["segments"]) == 2)
            socket.send_json(stop_message(settings))
            assert assert_socket_closed(socket) == 1000
        wait_for(client, lambda: client.app.state.transcription.active_count == 0
                 and client.app.state.media_capture.active_count == 0)
        exported = client.get(f"/api/transcripts/{PARENT}/export?format=json")
        assert exported.status_code == 200
        session = exported.json()["session"]
        assert session["status"] == "completed" and session["ended_at"]
        assert {(item["track"], item["text"]) for item in session["segments"]} == {
            ("inbound", "Caller words."), ("outbound", "Playback words.")}
        assert all(item["start_ms"] == 0 and item["end_ms"] == 20 for item in session["segments"])
        assert session["tracks"]["inbound"]["interim"] == ""
        assert session["tracks"]["outbound"]["meaning"] == "caller-playback"
        plain = client.get(f"/api/transcripts/{PARENT}/export?format=txt")
        assert "[00:00] Caller input: Caller words." in plain.text
        assert "[00:00] Caller playback: Playback words." in plain.text
        assert "fixture-private-key" not in exported.text + plain.text
        saved = json.loads((tmp_path / "transcripts" / f"{PARENT}.json").read_text())
        assert saved["segments"] == session["segments"]
        assert all(track["frames"] == 1 for track in read_manifest(settings)["tracks"].values())
        assert client.app.state.switchboard.active_count == 1
        assert gateway.ended_calls == []  # Ending passive streams never ends the human call.


def test_provider_failure_is_visible_while_capture_and_phone_call_continue(tmp_path):
    with live_client(tmp_path, options=[{"failure": True}]) as (client, settings, gateway, connector):
        _, token = stream_and_token(client, settings)
        viewer(client, settings)
        with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)) as socket:
            send_start(socket, settings, token)
            wait_for(client, lambda: len(connector.sockets) == 2
                     and client.app.state.transcription.active_count == 0)
            session = client.get("/api/transcripts").json()["sessions"][0]
            assert session["status"] == "failed"
            assert all(track["error"] == "provider-unavailable" for track in session["tracks"].values())
            assert "fixture-private-key" not in json.dumps(session)
            send_audio(socket, timestamp=0, chunk=1)
            send_audio(socket, timestamp=20, chunk=2)
            socket.send_json(stop_message(settings))
            assert assert_socket_closed(socket) == 1000
        wait_for(client, lambda: client.app.state.media_capture.active_count == 0)
        assert read_manifest(settings)["tracks"]["inbound"]["frames"] == 2
        assert client.app.state.switchboard.active_count == 1
        assert gateway.ended_calls == [] and gateway.ended_conferences == []


def test_deploy_waits_for_provider_final_flush_after_human_call_cleanup(tmp_path):
    with live_client(tmp_path, options=[{"close_result": False}]) as (client, settings, _, connector):
        _, token = stream_and_token(client, settings)
        with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)) as socket:
            send_start(socket, settings, token)
            send_audio(socket, track="inbound")
            send_audio(socket, track="outbound")
            wait_for(client, lambda: len(connector.sockets) == 2)
            socket.send_json(stop_message(settings))
            assert assert_socket_closed(socket) == 1000
        wait_for(client, lambda: all(any(isinstance(value, str) and json.loads(value)["type"] == "CloseStream"
                                        for value in provider.sent) for provider in connector.sockets))
        try:
            finish_call(client, settings)
            assert client.app.state.media_capture.active_count == 0
            assert client.app.state.media_capture.pending_count == 0
            assert client.app.state.switchboard.pending_count == 0
            headers = {"Authorization": "Bearer " + settings.deploy_control_token}
            draining = client.post("/internal/deploy", headers=headers, json={"draining": True}).json()
            assert draining == {"draining": True, "active_sessions": 0, "pending_work": 1}
        finally:
            # Release the fake provider only after proving final-result cleanup blocks deploy.
            for provider in connector.sockets:
                client.portal.call(provider.messages.put_nowait, None)
        wait_for(client, lambda: client.app.state.transcription.active_count == 0)
        assert client.get("/internal/deploy", headers=headers).json() == {
            "draining": True, "active_sessions": 0, "pending_work": 0}
