"""Browser Voice grants and service access remain scoped to one outbound call."""

from dataclasses import replace
from types import SimpleNamespace
import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import jwt
import pytest
from twilio.request_validator import RequestValidator

import config
from config import Settings
from app import create_app
from operator_service.browser_voice import browser_voice_identity, browser_voice_token
from workspace_auth import install_workspace_access
from workspace_auth.web import TWILIO_HTTP


BASE = Settings("AC" + "1" * 32, "offline-auth-token", "https://operator.example")
SESSION_ID = "a" * 32
ENABLED = replace(BASE, browser_voice_enabled=True, api_key="SK" + "2" * 32,
                  api_secret="offline-api-secret-" * 3,
                  twilio_browser_app_sid="AP" + "3" * 32,
                  twilio_conference_app_sid="AP" + "4" * 32)


def test_browser_voice_defaults_off_and_is_independent_of_phone_pilot():
    assert not BASE.browser_voice_enabled
    assert ENABLED.browser_voice_enabled
    assert not ENABLED.native_conference_enabled


@pytest.mark.parametrize("changes", [
    {"browser_voice_enabled": "true"},
    {"browser_voice_enabled": True},
    {"twilio_browser_app_sid": "APbad"},
    {"browser_voice_enabled": True, "api_key": "SK" + "2" * 32,
     "api_secret": "offline-secret", "twilio_browser_app_sid": "AP" + "3" * 32},
    {"browser_voice_enabled": True, "api_key": "SK" + "2" * 32,
     "api_secret": "offline-secret", "twilio_conference_app_sid": "AP" + "4" * 32},
    {"browser_voice_enabled": True, "api_key": "SK" + "2" * 32,
     "api_secret": "offline-secret", "twilio_conference_app_sid": "AP" + "4" * 32,
     "twilio_browser_app_sid": "AP" + "4" * 32},
])
def test_incomplete_or_conflicting_browser_configuration_fails_before_serving(changes):
    with pytest.raises(ValueError):
        replace(BASE, **changes)


def test_browser_configuration_from_environment_and_invalid_flag(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *_: None)
    values = {"TWILIO_ACCOUNT_SID": ENABLED.account_sid, "TWILIO_AUTH_TOKEN": ENABLED.auth_token,
              "PUBLIC_BASE_URL": ENABLED.public_base_url, "TWILIO_API_KEY": ENABLED.api_key,
              "TWILIO_API_SECRET": ENABLED.api_secret, "BROWSER_VOICE_ENABLED": "true",
              "TWILIO_BROWSER_APP_SID": ENABLED.twilio_browser_app_sid,
              "TWILIO_CONFERENCE_APP_SID": ENABLED.twilio_conference_app_sid,
              "NATIVE_CONFERENCE_ENABLED": "false"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.browser_voice_enabled and not settings.native_conference_enabled
    assert settings.twilio_browser_app_sid == ENABLED.twilio_browser_app_sid
    monkeypatch.setenv("BROWSER_VOICE_ENABLED", "yes")
    with pytest.raises(ValueError, match="BROWSER_VOICE_ENABLED"):
        Settings.from_env()


@pytest.mark.parametrize("max_seconds", [30, 1800, 14400])
def test_signed_browser_token_grants_one_outbound_app_and_covers_call_duration(max_seconds):
    settings = replace(ENABLED, max_call_seconds=max_seconds)
    token = browser_voice_token(settings, SimpleNamespace(id=SESSION_ID))
    payload = jwt.decode(token, settings.api_secret, algorithms=["HS256"], issuer=settings.api_key,
                         subject=settings.account_sid)
    assert payload["grants"] == {"identity": "phoney_" + SESSION_ID,
                                  "voice": {"outgoing": {"application_sid": settings.twilio_browser_app_sid}}}
    assert max_seconds + 90 <= payload["exp"] - int(time.time()) <= 24 * 60 * 60
    assert jwt.get_unverified_header(token)["cty"] == "twilio-fpa;v=1"
    assert settings.api_secret not in token and settings.auth_token not in token
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "wrong-offline-secret-" * 3, algorithms=["HS256"])


def test_tokens_require_enabled_feature_and_strict_session_identity():
    with pytest.raises(ValueError, match="disabled"):
        browser_voice_token(BASE, SimpleNamespace(id=SESSION_ID))
    for invalid in ("", "../other", "a" * 33, "A" * 32, None):
        with pytest.raises(ValueError, match="session identifier"):
            browser_voice_identity(invalid)
    assert browser_voice_identity("b" * 32) != browser_voice_identity(SESSION_ID)


@pytest.mark.parametrize("enabled", [False, True])
def test_public_health_reports_only_browser_feature_state_without_voice_secrets(tmp_path, enabled):
    settings = replace(ENABLED, browser_voice_enabled=enabled, workspace_access_enabled=True,
                       workspace_storage_dir=str(tmp_path))
    with TestClient(create_app(settings), base_url=settings.public_base_url) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["browser_voice_enabled"] is enabled
        for private in (settings.api_key, settings.api_secret, settings.auth_token,
                        settings.twilio_browser_app_sid, settings.twilio_conference_app_sid):
            assert private not in response.text


BROWSER_HOOKS = ("/twilio/browser-voice", "/twilio/browser-status",
                 f"/twilio/browser-status/{SESSION_ID}", f"/twilio/browser-finished/{SESSION_ID}")


@pytest.mark.parametrize("path", BROWSER_HOOKS)
def test_browser_service_exemptions_require_exact_post_and_valid_signature(tmp_path, path):
    settings = replace(BASE, workspace_access_enabled=True, workspace_storage_dir=str(tmp_path))
    app = FastAPI()
    access = install_workspace_access(app, settings)
    access.configure("642815", "test recovery phrase 924")

    async def hook(request: Request):
        return dict(await request.form())

    app.add_api_route(path, hook, methods=["POST", "GET"])
    form = {"AccountSid": settings.account_sid, "CallSid": "CA" + "5" * 32,
            "From": "client:phoney_" + SESSION_ID}
    signature = RequestValidator(settings.auth_token).compute_signature(settings.public_base_url + path, form)
    with TestClient(app, base_url=settings.public_base_url) as client:
        assert client.post(path, data=form).status_code == 403
        signed = {"X-Twilio-Signature": signature}
        response = client.post(path, data=form, headers=signed)
        assert response.status_code == 200 and response.json() == form
        assert response.headers["cache-control"] == "no-store"
        assert client.post(path, data={**form, "From": "client:other"}, headers=signed).status_code == 403
        assert client.get(path, headers=signed, follow_redirects=False).status_code == 303
        assert client.post(path + "/extra", data=form, headers=signed).status_code == 401
        # A signed provider request never unlocks the private token endpoint.
        assert client.post(f"/api/sessions/{SESSION_ID}/browser-token", data=form, headers=signed).status_code == 401
    assert TWILIO_HTTP.fullmatch(path)
    if SESSION_ID in path:
        assert not TWILIO_HTTP.fullmatch(path.replace(SESSION_ID, "invalid"))
