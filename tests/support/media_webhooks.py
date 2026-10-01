"""Shared fixtures and fakes for focused integration checks."""

import base64


import json


from pathlib import Path


import xml.etree.ElementTree as ET


from twilio.request_validator import RequestValidator


from config import Settings


PARENT = "CA" + "2" * 32


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


def send_audio(socket, *, track="inbound", timestamp=0, chunk=1, sample=0xFF):
    socket.send_json({"event": "media", "sequenceNumber": "2", "streamSid": STREAM,
                      "media": {"track": track, "chunk": str(chunk),
                                "timestamp": str(timestamp),
                                "payload": base64.b64encode(bytes([sample]) * 160).decode()}})


def read_manifest(settings):
    return json.loads((Path(settings.media_storage_dir) / PARENT / "manifest.json").read_text())
