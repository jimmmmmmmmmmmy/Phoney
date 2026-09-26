"""Voice-layer mistakes must fail at startup, not mid-call, and never leak keys."""

import pytest

from voice_stack.settings import VoiceSettings


LIVE = dict(gemini_api_key="test-gemini", elevenlabs_api_key="test-elevenlabs",
            elevenlabs_voice_id="21m00Tcm4TlvDq8ikWAM", output_dir="/tmp/voice")


def test_settings_default_to_inert_and_need_no_credentials():
    settings = VoiceSettings()
    assert not settings.enabled
    assert not settings.configured
    assert settings.missing == ["GEMINI_API_KEY", "ELEVENLABS_API_KEY",
                               "ELEVENLABS_VOICE_ID", "VOICE_OUTPUT_DIR"]
    assert settings.elevenlabs_model == "eleven_flash_v2_5"
    assert settings.twilio_ready


@pytest.mark.parametrize("fields", [
    {"enabled": "false"},
    {"enabled": 1},
    {"gemini_model": "models/gemini-3.8-flash"},
    {"gemini_model": "gemini-3.8-flash&key=leaked"},
    {"gemini_model": ""},
    {"elevenlabs_model": ""},
    {"elevenlabs_model": "eleven flash v2.5"},
    {"elevenlabs_output_format": "ulaw_8000&callback=https://other.example"},
    {"elevenlabs_output_format": "wav_8000"},
    {"elevenlabs_output_format": ""},
    {"elevenlabs_voice_id": "voice id with spaces"},
    {"elevenlabs_voice_id": "../../etc/passwd"},
    {"elevenlabs_voice_id": "x" * 65},
    {"output_dir": "relative/voice"},
    {"max_reply_tokens": 0},
    {"max_reply_tokens": 8193},
    {"max_reply_tokens": True},
    {"request_timeout": 0},
    {"request_timeout": 121},
    {"request_timeout": "20"},
    {"enabled": True},
])
def test_invalid_voice_configuration_fails(fields):
    with pytest.raises(ValueError):
        VoiceSettings(**fields)


def test_live_settings_require_every_credential_and_an_absolute_directory():
    assert VoiceSettings(enabled=True, **LIVE).configured
    assert not VoiceSettings(enabled=True, **LIVE).missing
    for key, value in (("gemini_api_key", ""), ("elevenlabs_api_key", ""),
                       ("elevenlabs_voice_id", ""), ("output_dir", "")):
        with pytest.raises(ValueError):
            VoiceSettings(enabled=True, **{**LIVE, key: value})


def test_settings_keep_credentials_out_of_repr():
    settings = VoiceSettings(enabled=True, gemini_api_key="private-gemini-key",
                             elevenlabs_api_key="private-elevenlabs-key",
                             elevenlabs_voice_id="21m00Tcm4TlvDq8ikWAM",
                             output_dir="/tmp/voice")
    assert "private-gemini-key" not in repr(settings)
    assert "private-elevenlabs-key" not in repr(settings)
    assert "21m00Tcm4TlvDq8ikWAM" in repr(settings)


def test_only_ulaw_8000_is_ready_for_twilio():
    assert VoiceSettings(elevenlabs_output_format="ulaw_8000").twilio_ready
    assert not VoiceSettings(elevenlabs_output_format="pcm_16000").twilio_ready


def test_from_env_reads_an_explicit_file(tmp_path):
    env_file = tmp_path / "voice.env"
    env_file.write_text("\n".join([
        "# owner voice for the demo",
        "VOICE_AGENT_ENABLED=true",
        "GEMINI_API_KEY=file-gemini",
        "ELEVENLABS_API_KEY=file-elevenlabs",
        "ELEVENLABS_VOICE_ID=c38kUX8pkfYO2kHyqfFy",
        f"VOICE_OUTPUT_DIR={tmp_path / 'turns'}",
        "VOICE_MAX_REPLY_TOKENS=512",
        "VOICE_REQUEST_TIMEOUT=15",
    ]))
    settings = VoiceSettings.from_env(env_file)
    assert settings.enabled
    assert settings.configured
    assert settings.elevenlabs_voice_id == "c38kUX8pkfYO2kHyqfFy"
    assert settings.gemini_model == "gemini-3.8-flash"
    assert settings.max_reply_tokens == 512
    assert settings.request_timeout == 15
    assert settings.output_path == tmp_path / "turns"


def test_from_env_without_a_file_uses_only_the_environment():
    settings = VoiceSettings.from_env(environ={
        "VOICE_AGENT_ENABLED": "FALSE", "GEMINI_API_KEY": "ambient-gemini",
        "ELEVENLABS_OUTPUT_FORMAT": "pcm_16000",
    })
    assert not settings.enabled
    assert settings.gemini_model == "gemini-3.8-flash"
    assert settings.elevenlabs_output_format == "pcm_16000"
    assert settings.elevenlabs_voice_id == ""


def test_from_env_rejects_an_unparsable_flag_and_a_missing_file(tmp_path):
    with pytest.raises(ValueError):
        VoiceSettings.from_env(environ={"VOICE_AGENT_ENABLED": "yes"})
    with pytest.raises(ValueError):
        VoiceSettings.from_env(tmp_path / "absent.env")


def test_from_env_an_explicit_file_overrides_ambient_values(monkeypatch, tmp_path):
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "ambient-voice-id")
    env_file = tmp_path / "voice.env"
    env_file.write_text("ELEVENLABS_VOICE_ID=c38kUX8pkfYO2kHyqfFy\n")
    assert VoiceSettings.from_env(env_file).elevenlabs_voice_id == "c38kUX8pkfYO2kHyqfFy"