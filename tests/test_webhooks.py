import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app import create_app
from config import Settings

SETTINGS = Settings("AC" + "1" * 32, "test-secret", "https://operator.example")
FORM = {"AccountSid": SETTINGS.account_sid, "CallSid": "CA" + "2" * 32,
        "From": "+15555550100", "To": "+15555550200"}


@pytest.fixture
def client():
    return TestClient(create_app(SETTINGS))


def signed_post(client, path="/voice", form=None, signing_url=None):
    form = FORM if form is None else form
    signature = RequestValidator(SETTINGS.auth_token).compute_signature(
        signing_url or SETTINGS.public_base_url + path, form)
    return client.post(path, data=form, headers={"X-Twilio-Signature": signature})


def test_valid_webhook_speaks_then_hangs_up(client):
    response = signed_post(client)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    xml = ET.fromstring(response.text)
    assert xml.tag == "Response"
    assert [child.tag for child in xml] == ["Say", "Hangup"]
    assert xml.find("Say").text == "New College Data Science Team"


def test_unsigned_and_modified_requests_rejected(client):
    assert client.post("/voice", data=FORM).status_code == 403
    signature = RequestValidator(SETTINGS.auth_token).compute_signature(
        SETTINGS.public_base_url + "/voice", FORM)
    assert client.post("/voice", data={**FORM, "To": "+15555550300"},
                       headers={"X-Twilio-Signature": signature}).status_code == 403


def test_signature_requires_exact_public_url_and_query(client):
    assert signed_post(client, signing_url="http://localhost:8000/voice").status_code == 403
    assert signed_post(client, path="/voice?test=hello%20world").status_code == 200
    assert signed_post(client, path="/voice?test=changed",
                       signing_url=SETTINGS.public_base_url + "/voice?test=original").status_code == 403


def test_new_twilio_fields_are_included_in_validation(client):
    assert signed_post(client, form={**FORM, "FutureTwilioField": "included"}).status_code == 200


def test_wrong_account_or_call_rejected(client):
    assert signed_post(client, form={**FORM, "AccountSid": "AC" + "3" * 32}).status_code == 403
    assert signed_post(client, form={**FORM, "CallSid": "not-a-call"}).status_code == 400


def test_routes_and_status(client):
    assert client.get("/health").json() == {"status": "ok", "service": "passive-operator", "build": 3,
                                           "switchboard_ready": False, "media_capture_enabled": False,
                                           "transcription_enabled": False, "voicemail_enabled": False}
    assert client.get("/voice").status_code == 405
    assert client.post("/voice", json=FORM).status_code == 415
    assert signed_post(client, "/status", {**FORM, "CallStatus": "completed"}).status_code == 204
    assert client.post("/status", data=FORM).status_code == 403


@pytest.mark.parametrize("url", ["http://example.com", "https://example.com/path", "",
                                  "https://user:pass@example.com", "https://example.com?x=1"])
def test_bad_public_origin_fails_closed(url):
    with pytest.raises(ValueError):
        Settings(SETTINGS.account_sid, SETTINGS.auth_token, url)
