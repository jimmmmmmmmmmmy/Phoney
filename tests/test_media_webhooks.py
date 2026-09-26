"""Signed HTTP/WebSocket checks for the passive Twilio audio tap.

The gateway is a local fake. These tests never place a phone call.
"""

import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path
import stat
import wave
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from app import create_app
from config import Settings


PARENT = "CA" + "2" * 32
OTHER = "CA" + "3" * 32
STREAM = "MZ" + "4" * 32
SETTINGS = Settings(
    account_sid="AC" + "1" * 32,
    auth_token="media-webhook-test-secret",
    public_base_url="https://operator.example",
    twilio_number="+15555550200",
    callee_number="+15555550100",
    deploy_control_token="media-deploy-control-token-32-characters",
)


class Gateway:
    def __init__(self):
        self.ended_calls = []
        self.ended_conferences = []

    async def end_call(self, sid):
        self.ended_calls.append(sid)

    async def end_conference(self, sid):
        self.ended_conferences.append(sid)


@pytest.fixture
def media_app(tmp_path):
    settings = replace(SETTINGS, media_capture_enabled=True,
                       media_storage_dir=str(tmp_path / "captures"))
    gateway = Gateway()
    with TestClient(create_app(settings, gateway=gateway)) as client:
        yield client, settings, gateway


def signed_post(client, settings, path, form):
    signature = RequestValidator(settings.auth_token).compute_signature(
        settings.public_base_url + path, form)
    return client.post(path, data=form, headers={"X-Twilio-Signature": signature})


def voice(client, settings):
    form = {"AccountSid": settings.account_sid, "CallSid": PARENT,
            "From": "+15555550300", "To": settings.twilio_number,
            "Direction": "inbound"}
    response = signed_post(client, settings, "/voice", form)
    assert response.status_code == 200
    return ET.fromstring(response.text)


def stream_and_token(client, settings):
    stream = voice(client, settings).find("Start/Stream")
    assert stream is not None
    token = next(p.attrib["value"] for p in stream.findall("Parameter")
                 if p.attrib["name"] == "token")
    return stream, token


def socket_headers(settings, path=None, scheme="wss"):
    path = path if path is not None else f"/media/{PARENT}/"
    url = settings.public_base_url.replace("https://", scheme + "://", 1) + path
    return {"X-Twilio-Signature": RequestValidator(settings.auth_token).compute_signature(url, {})}


def start_message(settings, token, **changes):
    start = {"accountSid": settings.account_sid, "callSid": PARENT,
             "streamSid": STREAM, "tracks": ["inbound", "outbound"],
             "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
             "customParameters": {"token": token}}
    start.update(changes)
    return {"event": "start", "sequenceNumber": "1", "streamSid": STREAM, "start": start}


def send_start(socket, settings, token, **changes):
    socket.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
    socket.send_json(start_message(settings, token, **changes))


def stop_message(settings):
    return {"event": "stop", "sequenceNumber": "4", "streamSid": STREAM,
            "stop": {"accountSid": settings.account_sid, "callSid": PARENT}}


def assert_socket_closed(socket):
    message = socket.receive()
    assert message["type"] == "websocket.close"
    return message.get("code")


def capture_files(settings):
    return [p for p in Path(settings.media_storage_dir).rglob("*") if p.is_file()]


def send_audio(socket, *, track="inbound", timestamp=0, chunk=1, sample=0xFF):
    socket.send_json({"event": "media", "sequenceNumber": "2", "streamSid": STREAM,
                      "media": {"track": track, "chunk": str(chunk),
                                "timestamp": str(timestamp),
                                "payload": base64.b64encode(bytes([sample]) * 160).decode()}})


def await_capture_finish(client):
    client.portal.call(client.app.state.media_capture.finish, PARENT, "stream-stopped")


def read_manifest(settings):
    return json.loads((Path(settings.media_storage_dir) / PARENT / "manifest.json").read_text())


def test_twiml_starts_both_tracks_before_conference_without_replacing_call(media_app):
    client, settings, gateway = media_app
    xml = voice(client, settings)
    tags = [element.tag for element in xml]
    assert tags.index("Say") < tags.index("Start") < tags.index("Dial")
    stream = xml.find("Start/Stream")
    assert stream.attrib["track"] == "both_tracks"
    assert stream.attrib["url"] == f"wss://operator.example/media/{PARENT}/"
    assert "?" not in stream.attrib["url"]
    assert stream.attrib["name"] == "capture-" + PARENT
    assert stream.attrib["statusCallback"] == f"https://operator.example/media/status/{PARENT}"
    assert stream.attrib.get("statusCallbackMethod", "POST") == "POST"
    params = {p.attrib["name"]: p.attrib["value"] for p in stream.findall("Parameter")}
    assert len(params["token"]) >= 32
    assert xml.find("Dial/Conference").text == "operator-" + PARENT
    assert gateway.ended_calls == []


def test_disabled_capture_keeps_existing_conference_flow(tmp_path):
    settings = replace(SETTINGS, media_capture_enabled=False,
                       media_storage_dir=str(tmp_path / "captures"))
    with TestClient(create_app(settings, gateway=Gateway())) as client:
        xml = voice(client, settings)
        assert xml.find("Start") is None
        assert xml.find("Dial/Conference") is not None
        assert client.get("/health").json()["media_capture_enabled"] is False
        assert capture_files(settings) == []


def test_health_exposes_capability_without_storage_path_or_tokens(media_app):
    client, settings, _ = media_app
    payload = client.get("/health").json()
    assert payload["build"] == 2
    assert payload["media_capture_enabled"] is True
    assert settings.auth_token not in json.dumps(payload)
    assert settings.media_storage_dir not in json.dumps(payload)
    assert "active_sessions" not in payload


def test_retried_voice_webhook_preserves_capture_ticket(media_app):
    client, settings, _ = media_app
    first_stream, first_token = stream_and_token(client, settings)
    second_stream, second_token = stream_and_token(client, settings)
    assert first_token == second_token
    assert first_stream.attrib == second_stream.attrib
    assert len(client.app.state.switchboard.sessions) == 1
    assert capture_files(settings) == []


@pytest.mark.parametrize("kind", ["missing", "incorrect", "https", "other_path", "query"])
def test_handshake_rejects_invalid_signature_or_query_before_recording(media_app, kind):
    client, settings, _ = media_app
    stream_and_token(client, settings)
    path = f"/media/{PARENT}/"
    headers = socket_headers(settings)
    if kind == "missing":
        headers = {}
    elif kind == "incorrect":
        headers = {"X-Twilio-Signature": "bad-signature"}
    elif kind == "https":
        headers = socket_headers(settings, scheme="https")
    elif kind == "other_path":
        headers = socket_headers(settings, f"/media/{OTHER}/")
    else:
        path += "?token=untrusted"
        headers = socket_headers(settings, path)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(path, headers=headers):
            pytest.fail("An invalid handshake was accepted")
    assert capture_files(settings) == []


def test_signed_socket_requires_a_live_known_session(media_app):
    client, settings, _ = media_app
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)):
            pytest.fail("Unknown call was accepted")
    assert capture_files(settings) == []


@pytest.mark.parametrize("changes", [
    {"customParameters": {"token": "incorrect-token"}},
    {"accountSid": "AC" + "9" * 32},
    {"callSid": OTHER},
    {"mediaFormat": {"encoding": "audio/pcm", "sampleRate": 8000, "channels": 1}},
    {"mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 16000, "channels": 1}},
])
def test_invalid_start_has_no_capture_and_does_not_end_conference(media_app, changes):
    client, settings, gateway = media_app
    _, token = stream_and_token(client, settings)
    with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)) as socket:
        send_start(socket, settings, token, **changes)
        socket.send_json(stop_message(settings))
        assert assert_socket_closed(socket) == 1008
    assert capture_files(settings) == []
    assert client.app.state.switchboard.sessions[PARENT].phase != "ended"
    assert gateway.ended_calls == []
    assert gateway.ended_conferences == []


def stream_status(settings, **changes):
    return {"AccountSid": settings.account_sid, "CallSid": PARENT,
            "StreamSid": STREAM, "StreamName": "capture-" + PARENT,
            "StreamEvent": "stream-error", **changes}


def test_stream_status_is_signed_and_bound_to_parent_call(media_app):
    client, settings, gateway = media_app
    stream_and_token(client, settings)
    path = f"/media/status/{PARENT}"
    assert client.post(path, data=stream_status(settings)).status_code == 403
    assert signed_post(client, settings, path, stream_status(
        settings, AccountSid="AC" + "9" * 32)).status_code == 403
    for changes in ({"CallSid": OTHER}, {"StreamSid": "bad"},
                    {"StreamEvent": "unknown"}, {"StreamName": ""}):
        assert signed_post(client, settings, path,
                           stream_status(settings, **changes)).status_code == 400
    assert client.app.state.switchboard.sessions[PARENT].phase != "ended"
    assert gateway.ended_calls == []


def test_provider_capture_error_leaves_conference_running_and_redacts_error(media_app, caplog):
    client, settings, gateway = media_app
    stream_and_token(client, settings)
    error_text = "private token secret123 and phone +15555559999"
    response = signed_post(client, settings, f"/media/status/{PARENT}", stream_status(
        settings, StreamError=error_text))
    assert response.status_code == 204
    assert client.app.state.switchboard.sessions[PARENT].phase != "ended"
    assert gateway.ended_calls == []
    assert error_text not in caplog.text


def test_valid_capture_writes_separate_private_wavs_with_timeline_gaps(media_app):
    client, settings, gateway = media_app
    _, token = stream_and_token(client, settings)
    headers = {**socket_headers(settings), "Host": "forged.example",
               "X-Forwarded-Host": "forged.example", "X-Forwarded-Proto": "http"}
    with client.websocket_connect(f"/media/{PARENT}/", headers=headers) as socket:
        send_start(socket, settings, token)
        send_audio(socket, sample=0x80)
        send_audio(socket, track="outbound", timestamp=20)
        send_audio(socket, timestamp=40, chunk=2)
        socket.send_json(stop_message(settings))
        assert assert_socket_closed(socket) == 1000
    await_capture_finish(client)
    manifest = read_manifest(settings)
    assert manifest["call_sid"] == PARENT
    assert manifest["stream_sid"] == STREAM
    assert manifest["status"] == "completed"
    assert manifest["encoding"] == "pcm_s16le"
    assert manifest["tracks"]["inbound"]["meaning"] == "caller-input"
    assert manifest["tracks"]["outbound"]["meaning"] == "caller-playback"
    assert token not in json.dumps(manifest)
    directory = Path(settings.media_storage_dir) / PARENT
    with wave.open(str(directory / "inbound.wav")) as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 8000)
        assert audio.getnframes() == 480
        pcm = audio.readframes(480)
        assert pcm[:320] != bytes(320)
        assert pcm[320:] == bytes(640)
    with wave.open(str(directory / "outbound.wav")) as audio:
        assert audio.getnframes() == 320
    for file in capture_files(settings):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert client.app.state.switchboard.sessions[PARENT].phase != "ended"
    assert gateway.ended_calls == []
    assert gateway.ended_conferences == []


def test_completed_ticket_cannot_be_replayed_or_overwrite_audio(media_app):
    client, settings, _ = media_app
    _, token = stream_and_token(client, settings)
    path = f"/media/{PARENT}/"
    with client.websocket_connect(path, headers=socket_headers(settings)) as socket:
        send_start(socket, settings, token)
        send_audio(socket)
        socket.send_json(stop_message(settings))
        assert_socket_closed(socket)
    await_capture_finish(client)
    original = {p: p.read_bytes() for p in capture_files(settings)}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(path, headers=socket_headers(settings)):
            pytest.fail("A completed capture ticket was reused")
    assert {p: p.read_bytes() for p in capture_files(settings)} == original


def test_conference_finish_finalizes_capture_and_clears_deploy_work(media_app):
    client, settings, _ = media_app
    _, token = stream_and_token(client, settings)
    headers = {"Authorization": "Bearer " + settings.deploy_control_token}
    with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)) as socket:
        send_start(socket, settings, token)
        send_audio(socket)

        async def wait_started():
            async with asyncio.timeout(2):
                while not client.app.state.media_capture.active_count:
                    await asyncio.sleep(0.01)

        client.portal.call(wait_started)
        running = client.get("/internal/deploy", headers=headers).json()
        assert running["active_sessions"] == 1
        assert running["pending_work"] >= 1
        response = signed_post(client, settings, f"/conference/finished/{PARENT}",
                               {"AccountSid": settings.account_sid, "CallSid": PARENT})
        assert response.status_code == 200
        assert "<Hangup" in response.text
        assert assert_socket_closed(socket) == 1000
    client.portal.call(client.app.state.switchboard.wait_idle)
    final = client.get("/internal/deploy", headers=headers).json()
    assert final["active_sessions"] == 0
    assert final["pending_work"] == 0
    assert read_manifest(settings)["status"] == "completed"


def test_storage_failure_does_not_end_the_human_call(media_app):
    client, settings, gateway = media_app
    path = Path(settings.media_storage_dir)
    path.write_text("A file deliberately blocks the recording directory.")
    _, token = stream_and_token(client, settings)
    with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)) as socket:
        send_start(socket, settings, token)
        send_audio(socket)
        socket.send_json(stop_message(settings))
        assert_socket_closed(socket)
    await_capture_finish(client)
    assert client.app.state.switchboard.sessions[PARENT].phase != "ended"
    assert gateway.ended_calls == []
    assert gateway.ended_conferences == []
