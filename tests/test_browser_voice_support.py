"""Focused product and boundary checks; test helpers live in support."""

from dataclasses import replace
from types import SimpleNamespace
import time

import jwt
import pytest

from config import Settings
from operator_service.browser_voice import browser_voice_token
import config

from support.browser_voice_support import ENABLED, SESSION_ID


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
    monkeypatch.delenv("NATIVE_CONFERENCE_JITTER_BUFFER", raising=False)
    settings = Settings.from_env()
    assert settings.browser_voice_enabled and not settings.native_conference_enabled
    assert settings.native_conference_jitter_buffer == "medium"
    assert settings.twilio_browser_app_sid == ENABLED.twilio_browser_app_sid
    monkeypatch.setenv("NATIVE_CONFERENCE_JITTER_BUFFER", " LARGE ")
    assert Settings.from_env().native_conference_jitter_buffer == "large"
    monkeypatch.setenv("NATIVE_CONFERENCE_JITTER_BUFFER", "tiny")
    with pytest.raises(ValueError, match="NATIVE_CONFERENCE_JITTER_BUFFER"):
        Settings.from_env()
    monkeypatch.setenv("NATIVE_CONFERENCE_JITTER_BUFFER", "medium")
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
