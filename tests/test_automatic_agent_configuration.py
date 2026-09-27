"""Explicit auto-agent opt-in and dependencies for the existing phone bridge."""

from dataclasses import replace

import pytest

from config import Settings
from test_operator_keypad import SETTINGS


def configured(tmp_path):
    return replace(SETTINGS, operator_inbound_enabled=True, agent_management_enabled=True,
                   workspace_storage_dir=str(tmp_path / "workspace"), media_capture_enabled=True,
                   media_storage_dir=str(tmp_path / "captures"), transcription_enabled=True,
                   deepgram_api_key="test-deepgram", transcript_storage_dir=str(tmp_path / "transcripts"),
                   modulate_detection_enabled=True, modulate_api_key="test-modulate",
                   detection_storage_dir=str(tmp_path / "detection"))


def test_manual_configuration_keeps_both_automatic_features_off_by_default(tmp_path):
    settings = configured(tmp_path)
    assert not settings.automatic_takeover_enabled and not settings.voicemail_agent_enabled
    assert settings.voicemail_agent_ring_seconds == 10
    assert replace(settings, automatic_takeover_enabled=True).automatic_takeover_enabled
    assert replace(settings, voicemail_agent_enabled=True).voicemail_agent_enabled


@pytest.mark.parametrize("field", ["automatic_takeover_enabled", "voicemail_agent_enabled"])
@pytest.mark.parametrize("invalid", ["true", 1, None])
def test_automatic_flags_require_booleans(tmp_path, field, invalid):
    with pytest.raises(ValueError, match=field.upper()):
        replace(configured(tmp_path), **{field: invalid})


@pytest.mark.parametrize("field", ["automatic_takeover_enabled", "voicemail_agent_enabled"])
def test_automatic_features_require_inbound_bridge(tmp_path, field):
    with pytest.raises(ValueError, match="OPERATOR_INBOUND_ENABLED"):
        replace(configured(tmp_path), operator_inbound_enabled=False, **{field: True})


def test_detection_takeover_requires_live_detection_but_voicemail_does_not(tmp_path):
    with pytest.raises(ValueError, match="live Modulate detection"):
        replace(configured(tmp_path), modulate_detection_enabled=False, automatic_takeover_enabled=True)
    settings = replace(configured(tmp_path), modulate_detection_enabled=False, voicemail_agent_enabled=True)
    assert settings.voicemail_agent_enabled and not settings.automatic_takeover_enabled


@pytest.mark.parametrize("seconds", [4, 61, True, 15.5, "15", None])
def test_voicemail_ring_deadline_is_bounded_integer(tmp_path, seconds):
    with pytest.raises(ValueError, match="VOICEMAIL_AGENT_RING_SECONDS"):
        replace(configured(tmp_path), voicemail_agent_ring_seconds=seconds)


def test_env_requires_explicit_flags_and_reads_ring_deadline(monkeypatch, tmp_path):
    env = {
        "TWILIO_ACCOUNT_SID": SETTINGS.account_sid, "TWILIO_AUTH_TOKEN": SETTINGS.auth_token,
        "PUBLIC_BASE_URL": SETTINGS.public_base_url, "TWILIO_NUMBER": SETTINGS.twilio_number,
        "OWNER_NUMBER": SETTINGS.owner_number, "OPERATOR_ADMIN_TOKEN": SETTINGS.operator_admin_token,
        "OPERATOR_INBOUND_ENABLED": "true", "VOICE_AGENT_ENABLED": "true",
        "AGENT_MANAGEMENT_ENABLED": "true", "WORKSPACE_STORAGE_DIR": str(tmp_path / "workspace"),
        "MEDIA_CAPTURE_ENABLED": "true", "MEDIA_STORAGE_DIR": str(tmp_path / "capture"),
        "TRANSCRIPTION_ENABLED": "true", "DEEPGRAM_API_KEY": "test-deepgram",
        "TRANSCRIPT_STORAGE_DIR": str(tmp_path / "transcripts"), "MODULATE_DETECTION_ENABLED": "true",
        "MODULATE_API_KEY": "test-modulate", "DETECTION_STORAGE_DIR": str(tmp_path / "detection"),
    }
    monkeypatch.setattr("config.load_dotenv", lambda *args: None)
    monkeypatch.setattr("config.os.getenv", lambda name, default=None: env.get(name, default))
    settings = Settings.from_env()
    assert not settings.automatic_takeover_enabled and not settings.voicemail_agent_enabled
    env.update(AUTOMATIC_TAKEOVER_ENABLED="true", VOICEMAIL_AGENT_ENABLED="true",
               VOICEMAIL_AGENT_RING_SECONDS="12")
    settings = Settings.from_env()
    assert settings.automatic_takeover_enabled and settings.voicemail_agent_enabled
    assert settings.voicemail_agent_ring_seconds == 12
    for name in ("AUTOMATIC_TAKEOVER_ENABLED", "VOICEMAIL_AGENT_ENABLED"):
        env[name] = "perhaps"
        with pytest.raises(ValueError, match=name):
            Settings.from_env()
        env[name] = "true"
