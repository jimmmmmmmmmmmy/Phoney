"""Configuration mistakes must fail before an outbound call can be reserved."""

import pytest

from config import Settings


BASE = dict(account_sid="AC" + "1" * 32, auth_token="test-auth",
            public_base_url="https://operator.example")


@pytest.mark.parametrize("fields", [
    {"callee_number": "+15555550100"},
    {"twilio_number": "+15555550100", "callee_number": "+15555550100"},
    {"callee_number": "3125550100", "twilio_number": "+15555550101"},
    {"twilio_number": "not-a-phone"},
    {"api_key": "SK" + "2" * 32},
    {"api_secret": "secret"},
    {"api_key": "invalid", "api_secret": "secret"},
    {"switchboard_setup_timeout": 0},
    {"switchboard_setup_timeout": 121},
    {"deploy_control_token": "short"},
    {"media_capture_enabled": True},
    {"media_capture_enabled": True, "media_storage_dir": "relative/recordings"},
    {"media_capture_enabled": "false"},
    {"media_max_seconds": 0},
    {"media_max_seconds": 3601},
    {"media_max_seconds": 1.5},
    {"media_max_seconds": True},
    {"transcription_enabled": "false"},
    {"transcription_enabled": True},
    {"deepgram_model": "nova-3&callback=https://other.example"},
    {"voicemail_enabled": "false"},
    {"voicemail_enabled": True},
    {"voicemail_enabled": True, "voicemail_storage_dir": "relative"},
    {"voicemail_max_seconds": 1},
    {"voicemail_max_seconds": 601},
    {"voicemail_max_seconds": True},
    {"gemini_summary_model": "../another-host?key=value"},
    {"owner_number": "12025550101"},
    {"allowed_destinations": ("+12025550103", "12025550103")},
    {"allowed_destinations": ("+12025550103", "+12025550103")},
    {"allowed_destination_countries": ("US", "US")},
    {"allowed_destination_countries": ("CA",)},
    {"allowed_destination_countries": ("US", "GB")},
    {"allowed_destination_countries": ("us",)},
    {"allowed_destination_countries": "US"},
    {"allowed_destination_countries": True},
    {"twilio_number": "+15555550100", "allowed_destinations": ("+15555550100",)},
    {"owner_number": "+15555550100", "twilio_number": "+15555550101",
     "allowed_destinations": ("+15555550100",)},
    {"operator_admin_token": "too-short"},
    {"public_calling_enabled": "true"},
    {"public_calling_enabled": 1},
    {"max_call_seconds": 29},
    {"max_call_seconds": 14401},
    {"max_call_seconds": True},
    {"voice_agent_enabled": "true"},
    {"agent_management_enabled": "true"},
    {"agent_demo_mode": "true"},
    {"agent_demo_mode": True},
    {"operator_inbound_enabled": "true"},
    {"agent_management_enabled": True},
    {"operator_inbound_enabled": True},
])
def test_invalid_switchboard_configuration_fails(fields):
    with pytest.raises(ValueError):
        Settings(**BASE, **fields)


def test_configuration_keeps_sensitive_values_out_of_repr():
    settings = Settings(**BASE, twilio_number="+15555550100", callee_number="+15555550101",
                        api_key="SK" + "2" * 32, api_secret="private-rest-secret",
                        deploy_control_token="private-deploy-token-" * 3,
                        deepgram_api_key="private-deepgram-key", gemini_api_key="private-gemini-key")
    assert settings.switchboard_ready
    for field in (settings.auth_token, settings.api_key, settings.api_secret,
                  settings.twilio_number, settings.callee_number, settings.deploy_control_token,
                  settings.deepgram_api_key, settings.gemini_api_key):
        assert field not in repr(settings)


def test_manual_inbound_configuration_keeps_automatic_behavior_off(tmp_path):
    fields = dict(owner_number="+15555550100", twilio_number="+15555550101",
                  operator_admin_token="operator-admin-token-" * 2,
                  voice_agent_enabled=True, agent_management_enabled=True,
                  operator_inbound_enabled=True, workspace_storage_dir=str(tmp_path / "workspace"),
                  media_capture_enabled=True, media_storage_dir=str(tmp_path / "audio"),
                  transcription_enabled=True, deepgram_api_key="fake-deepgram",
                  transcript_storage_dir=str(tmp_path / "transcripts"))
    configured = Settings(**BASE, **fields)
    assert configured.operator_ready and configured.allowed_destinations == ()
    for field in ("voice_agent_enabled", "agent_management_enabled", "transcription_enabled"):
        with pytest.raises(ValueError):
            Settings(**BASE, **{**fields, field: False})
    defaults = Settings(**BASE)
    assert not defaults.voice_agent_enabled
    assert not defaults.operator_inbound_enabled
    assert not defaults.agent_management_enabled
    assert not defaults.agent_demo_mode


def test_demo_agent_editing_does_not_enable_phone_takeover(tmp_path):
    configured = Settings(**BASE, agent_demo_mode=True, agent_management_enabled=True,
                          workspace_storage_dir=str(tmp_path))
    assert configured.agent_demo_mode
    assert not configured.voice_agent_enabled
    assert not configured.operator_inbound_enabled


def test_transcription_requires_provider_key_and_storage_but_no_viewer_code(tmp_path):
    complete = dict(media_capture_enabled=True, media_storage_dir=str(tmp_path / "audio"),
                    transcription_enabled=True, deepgram_api_key="test-deepgram",
                    transcript_storage_dir=str(tmp_path / "text"))
    assert Settings(**BASE, **complete).transcription_enabled
    for key, value in (("deepgram_api_key", ""), ("transcript_storage_dir", "relative"),
                       ("media_capture_enabled", False)):
        with pytest.raises(ValueError):
            Settings(**BASE, **{**complete, key: value})


def test_the_operator_bridge_needs_an_owner_number_a_token_and_an_allowlist():
    bridge = dict(owner_number="+15555550100", twilio_number="+15555550101",
                  operator_admin_token="operator-admin-token-" * 2,
                  allowed_destinations=("+15555550102", "+15555550103"))
    settings = Settings(**BASE, **bridge)
    assert settings.operator_ready is True
    assert settings.max_call_seconds == 1800 and settings.voice_agent_enabled is False
    for field in (settings.owner_number, settings.operator_admin_token):
        assert field not in repr(settings)
    # Every value is optional: an empty bridge leaves the conference path alone.
    assert Settings(**BASE).operator_ready is False
    for key, value in (("owner_number", ""), ("operator_admin_token", ""),
                       ("allowed_destinations", ()), ("twilio_number", "")):
        assert Settings(**BASE, **{**bridge, key: value}).operator_ready is False


def test_country_dialing_requires_explicit_us_configuration(monkeypatch):
    env = {"TWILIO_ACCOUNT_SID": BASE["account_sid"], "TWILIO_AUTH_TOKEN": BASE["auth_token"],
           "PUBLIC_BASE_URL": BASE["public_base_url"], "OWNER_NUMBER": "+12025550101",
           "TWILIO_NUMBER": "+12025550102", "OPERATOR_ADMIN_TOKEN": "operator-admin-token-" * 2}
    monkeypatch.setattr("config.load_dotenv", lambda *args: None)
    monkeypatch.setattr("config.os.getenv", lambda name, default=None: env.get(name, default))
    assert Settings.from_env().allowed_destination_countries == ()
    assert Settings.from_env().public_calling_enabled is False
    assert Settings.from_env().operator_ready is False
    env["ALLOWED_DESTINATION_COUNTRIES"] = " us "
    configured = Settings.from_env()
    assert configured.allowed_destination_countries == ("US",)
    assert configured.allowed_destinations == ()
    assert configured.operator_ready is True
    env["ALLOWED_DESTINATION_COUNTRIES"] = "US,CA"
    with pytest.raises(ValueError, match="ALLOWED_DESTINATION_COUNTRIES"):
        Settings.from_env()


def test_public_calling_requires_explicit_boolean_env_flag(monkeypatch):
    env = {"TWILIO_ACCOUNT_SID": BASE["account_sid"], "TWILIO_AUTH_TOKEN": BASE["auth_token"],
           "PUBLIC_BASE_URL": BASE["public_base_url"]}
    monkeypatch.setattr("config.load_dotenv", lambda *args: None)
    monkeypatch.setattr("config.os.getenv", lambda name, default=None: env.get(name, default))
    assert Settings.from_env().public_calling_enabled is False
    env["PUBLIC_CALLING_ENABLED"] = " true "
    assert Settings.from_env().public_calling_enabled is True
    env["PUBLIC_CALLING_ENABLED"] = "false"
    assert Settings.from_env().public_calling_enabled is False
    env["PUBLIC_CALLING_ENABLED"] = "yes"
    with pytest.raises(ValueError, match="PUBLIC_CALLING_ENABLED"):
        Settings.from_env()
