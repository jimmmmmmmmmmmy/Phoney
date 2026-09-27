"""Detection opt-in, secret handling, and bounded configuration."""

from dataclasses import replace
import pytest
from config import Settings

BASE = dict(account_sid="AC" + "1" * 32, auth_token="test-auth",
            public_base_url="https://operator.example")


def configured(tmp_path):
    return Settings(**BASE, media_capture_enabled=True,
        media_storage_dir=str(tmp_path / "audio"), modulate_detection_enabled=True,
        modulate_api_key="private-test-modulate-key", detection_storage_dir=str(tmp_path / "detection"))


def test_detection_is_opt_in_and_key_is_redacted(tmp_path):
    assert not Settings(**BASE).modulate_detection_enabled
    settings = configured(tmp_path)
    assert settings.modulate_api_key not in repr(settings)
    assert settings.modulate_detection_enabled


@pytest.mark.parametrize("field,value", [
    ("modulate_detection_enabled", "true"), ("media_capture_enabled", False),
    ("modulate_api_key", ""), ("modulate_api_key", "REPLACE_ME"),
    ("detection_storage_dir", ""), ("detection_storage_dir", "relative"),
    ("detection_storage_dir", "/"), ("detection_storage_dir", "/tmp/../var/detection"),
    ("modulate_detection_max_audio_seconds", True), ("modulate_detection_max_audio_seconds", 121),
    ("modulate_detection_max_audio_seconds", 0), ("modulate_detection_deadline_seconds", float("nan")),
    ("modulate_detection_deadline_seconds", float("inf")), ("modulate_detection_deadline_seconds", 0),
    ("modulate_detection_deadline_seconds", True), ("modulate_detection_min_confidence", 0.49),
    ("modulate_detection_min_confidence", 1.01), ("modulate_detection_min_confidence", float("nan")),
    ("modulate_detection_queue_frames", 0), ("modulate_detection_queue_frames", 1001),
])
def test_detection_rejects_incomplete_or_unbounded_configuration(tmp_path, field, value):
    with pytest.raises(ValueError):
        replace(configured(tmp_path), **{field: value})


def test_detection_storage_must_not_share_existing_call_record_directory(tmp_path):
    settings = configured(tmp_path)
    with pytest.raises(ValueError):
        replace(settings, call_details_storage_dir=settings.detection_storage_dir)


def test_env_reads_detection_key_without_enabling_by_key_alone(monkeypatch, tmp_path):
    monkeypatch.setattr("config.load_dotenv", lambda *args: None)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", BASE["account_sid"])
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", BASE["auth_token"])
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE["public_base_url"])
    monkeypatch.setenv("MODULATE_API_KEY", "private-env-key")
    monkeypatch.setenv("MODULATE_DETECTION_ENABLED", "false")
    settings = Settings.from_env()
    assert not settings.modulate_detection_enabled
    assert settings.modulate_api_key == "private-env-key"
    monkeypatch.setenv("MODULATE_DETECTION_ENABLED", "maybe")
    with pytest.raises(ValueError):
        Settings.from_env()
