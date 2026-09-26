"""Exercise authenticated admission control without live Twilio requests."""

from dataclasses import replace
from fastapi.testclient import TestClient
import pytest
from twilio.request_validator import RequestValidator

from app import create_app
from config import Settings


SETTINGS = Settings("AC" + "1" * 32, "test-auth", "https://operator.example",
                    twilio_number="+15555550100", callee_number="+15555550101",
                    deploy_control_token="test-deploy-control-token-32-characters")
HEADERS = {"Authorization": "Bearer " + SETTINGS.deploy_control_token}


class Gateway:
    async def end_call(self, sid):
        pass

    async def end_conference(self, sid):
        pass


def test_drain_authentication_and_read_only_status():
    with TestClient(create_app(SETTINGS, gateway=Gateway())) as client:
        assert client.get("/internal/deploy").status_code == 403
        assert client.post("/internal/deploy", json={"draining": True}).status_code == 403
        result = client.get("/internal/deploy", headers=HEADERS)
        assert result.json() == {"draining": False, "active_sessions": 0, "pending_work": 0}
        assert result.headers["cache-control"] == "no-store"


def test_disabled_control_fails_closed():
    with TestClient(create_app(replace(SETTINGS, deploy_control_token=""))) as client:
        assert client.post("/internal/deploy", headers=HEADERS,
                           json={"draining": True}).status_code == 503


@pytest.mark.parametrize("body", [{"draining": "false"}, {"draining": 1}, {}, [],
                                   {"draining": True, "extra": "value"}])
def test_drain_requires_explicit_boolean(body):
    with TestClient(create_app(SETTINGS)) as client:
        assert client.post("/internal/deploy", headers=HEADERS, json=body).status_code == 400


def test_draining_rejects_new_sessions_and_resumes_after_cancel():
    with TestClient(create_app(SETTINGS, gateway=Gateway())) as client:
        form = {"AccountSid": SETTINGS.account_sid, "CallSid": "CA" + "3" * 32,
                "From": "+15555550102", "To": SETTINGS.twilio_number}
        sig = RequestValidator(SETTINGS.auth_token).compute_signature(
            SETTINGS.public_base_url + "/voice", form)
        assert client.post("/internal/deploy", headers=HEADERS,
                           json={"draining": True}).json()["draining"]
        response = client.post("/voice", data=form, headers={"X-Twilio-Signature": sig})
        assert "<Hangup" in response.text and "<Conference" not in response.text
        assert not client.app.state.switchboard.sessions
        client.post("/internal/deploy", headers=HEADERS, json={"draining": False})
        response = client.post("/voice", data=form, headers={"X-Twilio-Signature": sig})
        assert "<Conference" in response.text
        assert client.get("/internal/deploy", headers=HEADERS).json()["active_sessions"] == 1
