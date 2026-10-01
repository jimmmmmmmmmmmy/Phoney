"""Shared fixtures and fakes for focused integration checks."""

import asyncio


from contextlib import contextmanager


from dataclasses import replace


import uuid


import xml.etree.ElementTree as ET


from fastapi.testclient import TestClient


import pytest


from app import create_app


from operator_service.sessions import CONNECTED, ENDED, OWNER, REMOTE, TOKEN_SECONDS


from support.media_webhooks import Gateway, signed_post


from support.operator_routes import (
    DESTINATION,
    HEADERS,
    OWNER_NUMBER,
    OWNER_SID,
    REMOTE_SID,
    SETTINGS,
    Dialer,
    session_of,
    settle,
    start_call,
    until,
)


from workspace_auth.web import SESSION_COOKIE


SDK_SETTINGS = replace(SETTINGS, browser_voice_enabled=True,
    api_key="SK" + "9" * 32, api_secret="local-test-api-secret-32-characters",
    twilio_browser_app_sid="AP" + "8" * 32,
    twilio_conference_app_sid="AP" + "7" * 32)


CONFERENCE = "CF" + "6" * 32


OTHER_SID = "CA" + "5" * 32


class SDKDialer(Dialer):
    def __init__(self):
        super().__init__()
        self.sid = REMOTE_SID
        self.mutes, self.ended_conferences = [], []

    async def mute_participant(self, conference_sid, call_sid, muted):
        self.mutes.append((conference_sid, call_sid, muted))

    async def end_conference(self, conference_sid):
        self.ended_conferences.append(conference_sid)


@contextmanager
def sdk_client(settings=SDK_SETTINGS):
    dialer = SDKDialer()
    with TestClient(create_app(settings, gateway=Gateway(), operator_dialer=dialer),
                    base_url=settings.public_base_url) as client:
        yield client, dialer, settings


def reserve(client, *, to=DESTINATION, key=None, headers=HEADERS):
    return client.post("/api/calls/outbound",
        headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())},
        json={"to": to, "browser_audio": True})


def grant(client, call):
    response = client.post(f"/api/sessions/{call.id}/browser-token", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    value = response.json()
    assert value["transport"] == "twilio-voice-sdk"
    assert set(value["params"]) == {"SessionId", "Token"}
    assert value["params"]["SessionId"] == call.id
    return value


def app_form(call, value=None, settings=SDK_SETTINGS, **updates):
    form = {"AccountSid": settings.account_sid, "CallSid": OWNER_SID,
            "From": "client:phoney_" + call.id,
            "ApplicationSid": settings.twilio_browser_app_sid}
    if value is not None:
        form.update(value["params"])
    form.update(updates)
    return form


def accepted(client, dialer, settings=SDK_SETTINGS):
    response = reserve(client)
    assert response.status_code == 202, response.text
    assert response.json()["audio_path"] == "native-conference"
    call = session_of(client, response.json()["session_id"])
    value = grant(client, call)
    response = signed_post(client, settings, "/twilio/browser-voice", app_form(call, value, settings))
    assert response.status_code == 200, response.text
    until(client, lambda: bool(call.legs[REMOTE].call_sid))
    return call, value, response


def conference_event(client, settings, call, role, *, event="participant-join", sequence=1):
    return signed_post(client, settings, f"/twilio/native-conference/{call.id}", {
        "AccountSid": settings.account_sid, "ConferenceSid": CONFERENCE,
        "FriendlyName": "phoney-" + call.id, "ParticipantLabel": role,
        "CallSid": call.legs[role].call_sid, "StatusCallbackEvent": event,
        "SequenceNumber": str(sequence)})
