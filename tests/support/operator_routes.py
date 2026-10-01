"""Shared fixtures and fakes for focused integration checks."""

import asyncio


import base64


from contextlib import contextmanager


import json


import time


import uuid


from fastapi.testclient import TestClient


from app import create_app


from config import Settings


from operator_service.sessions import CONNECTED, ENDED, HUMAN, OWNER, OWNER_PROMPT, REMOTE


from support.media_webhooks import Gateway, signed_post, socket_headers


ACCOUNT = "AC" + "a" * 32


OWNER_NUMBER = "+12025550101"


CALLEE = "+12025550102"


DESTINATION = "+12025550103"


ADMIN_TOKEN = "operator-admin-token-32-characters-long"


OWNER_SID = "CA" + "1" * 32


REMOTE_SID = "CA" + "2" * 32


OWNER_STREAM = "MZ" + "3" * 32


REMOTE_STREAM = "MZ" + "4" * 32


OWNER_FRAME = bytes([0x11]) * 160


REMOTE_FRAME = bytes([0x22]) * 160


SETTINGS = Settings(
    account_sid=ACCOUNT, auth_token="operator-route-test-token",
    public_base_url="https://operator.example", twilio_number=CALLEE,
    owner_number=OWNER_NUMBER, allowed_destinations=(DESTINATION,),
    operator_admin_token=ADMIN_TOKEN, max_call_seconds=1800,
    deploy_control_token="operator-deploy-control-token-32-chars",
)


HEADERS = {"Authorization": "Bearer " + ADMIN_TOKEN}


class Dialer:
    """A fake Twilio REST surface: records legs, never places a call."""

    def __init__(self):
        self.created = []
        self.ended = []
        self.hold = None
        self.failure = None
        self.sid = None

    async def create_leg(self, *, to, twiml, status_callback):
        call_sid = self.sid or "CA" + format(len(self.created) + 1, "032x")
        self.created.append({"to": to, "twiml": twiml, "status_callback": status_callback,
                             "sid": call_sid})
        if self.hold is not None:
            await self.hold.wait()
        if self.failure is not None:
            raise self.failure
        return call_sid

    async def end_call(self, call_sid):
        self.ended.append(call_sid)


@contextmanager
def bridge_client(settings=None, voice=None):
    settings = settings or SETTINGS
    dialer = Dialer()
    with TestClient(create_app(settings, gateway=Gateway(), operator_dialer=dialer,
                               operator_voice=voice),
                    base_url=settings.public_base_url) as client:
        yield client, dialer, settings


def start_call(client, key=None, to=DESTINATION):
    return client.post("/api/calls/outbound", headers={**HEADERS, "Idempotency-Key": key or str(uuid.uuid4())},
                       json={"to": to, "goal": "Ask for an itemized out-of-the-door quote"})


def settle(client):
    client.portal.call(client.app.state.operator.wait_idle)


def until(client, predicate, timeout=2.0):
    async def wait():
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)

    client.portal.call(wait)


def session_of(client, session_id):
    return client.app.state.operator.sessions[session_id]


def next_event(client, socket, timeout=2.0):
    """One decoded server-sent Twilio event, or None if nothing arrived in time."""
    stream = getattr(socket, "_send_rx", None)
    if stream is None:
        return json.loads(socket.receive_text())

    async def bounded():
        try:
            return await asyncio.wait_for(stream.receive(), timeout)
        except Exception:
            return None

    try:
        message = client.portal.call(bounded)
    except Exception:
        return None
    if message is None or not isinstance(message.get("text"), str):
        return None
    try:
        return json.loads(message["text"])
    except ValueError:
        return None


def await_frame(client, socket, expected, timeout=2.0):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("the expected audio frame never arrived")
        message = next_event(client, socket, timeout=min(0.5, remaining))
        if message is None:
            continue
        if message.get("event") == "media":
            if base64.b64decode(message["media"]["payload"]) == expected:
                return message


def connected():
    return {"event": "connected", "protocol": "Call", "version": "1.0.0"}


def start_message(client, session_id, role, **changes):
    leg = session_of(client, session_id).legs[role]
    stream_sid = OWNER_STREAM if role == OWNER else REMOTE_STREAM
    start = {"accountSid": ACCOUNT, "callSid": leg.call_sid, "streamSid": stream_sid,
             "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
             "customParameters": {"generation": str(leg.generation), "token": leg.token}}
    start.update(changes)
    return {"event": "start", "sequenceNumber": "1", "streamSid": stream_sid, "start": start}


def media_message(frame, role=OWNER, sequence="2"):
    return {"event": "media", "sequenceNumber": sequence,
            "streamSid": OWNER_STREAM if role == OWNER else REMOTE_STREAM,
            "media": {"track": "inbound", "payload": base64.b64encode(frame).decode("ascii")}}


def keypad(digit, sequence):
    return {"event": "dtmf", "sequenceNumber": str(sequence), "streamSid": OWNER_STREAM,
            "dtmf": {"track": "inbound_track", "digit": digit}}


def connect(client, settings, session_id, role):
    path = f"/media/{session_id}/{role}/"
    return client.websocket_connect(path, headers=socket_headers(settings, path=path))


def accept_owner(client, settings, session_id, socket, **changes):
    socket.send_json(connected())
    socket.send_json(start_message(client, session_id, OWNER, **changes))
    until(client, lambda: session_of(client, session_id).phase == OWNER_PROMPT)
