"""Signed inbound webhooks through real REST adapters; all Twilio I/O is mocked."""
from dataclasses import replace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import create_app
from caller_id import forwarding_identity
from operator_service.routes import TwilioLegs
from switchboard.gateway import TwilioGateway
from test_media_webhooks import signed_post
from test_operator_routes import SETTINGS as OPERATOR_SETTINGS, until
from test_switchboard import SETTINGS, VOICE, PARENT, OUTBOUND, CONFERENCE, conference_form

TOKEN = "test-opaque-forwarding-token"


def inbound_settings(tmp_path):
    return replace(OPERATOR_SETTINGS, operator_inbound_enabled=True,
        voice_agent_enabled=True, agent_management_enabled=True,
        media_capture_enabled=True, media_storage_dir=str(tmp_path / "audio"),
        transcription_enabled=True, deepgram_api_key="offline-key",
        transcript_storage_dir=str(tmp_path / "transcripts"),
        workspace_storage_dir=str(tmp_path / "workspace"))


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("token", [TOKEN, ""])
def test_signed_forwarding_preserves_identity_and_duplicate_does_not_redial(operator, token, tmp_path):
    settings = inbound_settings(tmp_path) if operator else SETTINGS
    rest = Mock()
    rest.calls.create.return_value.sid = OUTBOUND
    rest.conferences.return_value.participants.create.return_value.call_sid = OUTBOUND
    adapter = TwilioLegs(settings) if operator else TwilioGateway(settings)
    adapter._client = lambda: rest
    kwargs = {"operator_dialer": adapter} if operator else {"gateway": adapter}
    form = {**VOICE, "AccountSid": settings.account_sid, "To": settings.twilio_number,
            "CallToken": token}
    create = rest.calls.create if operator else rest.conferences.return_value.participants.create
    with TestClient(create_app(settings, **kwargs)) as client:
        assert signed_post(client, settings, "/voice", form).status_code == 200
        # Replayed webhook cannot replace the identity on an existing session.
        assert signed_post(client, settings, "/voice", {
            **form, "From": "+15555550400", "CallToken": "replacement-token"
        }).status_code == 200
        if not operator:
            for _ in range(2):
                assert signed_post(client, settings, f"/conference/events/{PARENT}",
                                   conference_form()).status_code == 204
            client.portal.call(client.app.state.switchboard.wait_idle)
        else:
            until(client, lambda: create.call_count == 1)
        create.assert_called_once()
        args = create.call_args.kwargs
        assert args["from_"] == (form["From"] if token else settings.twilio_number)
        assert args.get("call_token", "") == token
        assert args["to"] == (settings.owner_number if operator else settings.callee_number)
        if not operator:
            session = client.app.state.switchboard.sessions[PARENT]
        else:
            session = next(iter(client.app.state.operator.sessions.values()))
            assert "call_token" not in session.to_status()
        assert TOKEN not in repr(session)


@pytest.mark.parametrize("caller,token", [("anonymous", TOKEN), ("", TOKEN),
                                          ("+15555550300", "")])
def test_unavailable_identity_falls_back_to_owned_number(caller, token):
    assert forwarding_identity(SETTINGS.twilio_number, caller, token) == {
        "from_": SETTINGS.twilio_number}


@pytest.mark.parametrize("operator", [False, True])
def test_unsigned_webhook_cannot_forward(operator, tmp_path):
    settings = inbound_settings(tmp_path) if operator else SETTINGS
    adapter = Mock()
    kwargs = {"operator_dialer": adapter} if operator else {"gateway": adapter}
    with TestClient(create_app(settings, **kwargs)) as client:
        assert client.post("/voice", data={**VOICE, "CallToken": TOKEN}).status_code == 403
        adapter.create_leg.assert_not_called()
        adapter.create_participant.assert_not_called()


def test_forwarded_identity_authenticates_status_before_rest_response():
    import asyncio
    from switchboard import Switchboard
    from test_switchboard_engine import Gateway

    async def run():
        board = Switchboard(SETTINGS, gateway=Gateway())
        session = await board.start(PARENT, VOICE["From"], TOKEN)
        session.dial_reserved = True
        await board.call_status(PARENT, {
            "CallSid": OUTBOUND, "CallStatus": "in-progress",
            "From": VOICE["From"], "To": SETTINGS.callee_number,
            "Direction": "outbound-api",
        })
        assert session.outbound_sid == OUTBOUND
        assert session.callee_answered
        await board.close()

    asyncio.run(run())
