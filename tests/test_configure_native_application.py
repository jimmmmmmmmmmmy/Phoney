"""Deployment webhook synchronization using fake SDK resources only."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import configure_twilio
from test_switchboard import SETTINGS


APP_SID = "AP" + "b" * 32


@pytest.fixture
def configured(tmp_path, monkeypatch):
    settings = replace(SETTINGS, native_conference_enabled=True,
                       twilio_conference_app_sid=APP_SID)
    number = SimpleNamespace(sid="PN" + "1" * 32, phone_number=settings.twilio_number,
        capabilities={"voice": True}, voice_application_sid="", trunk_sid="",
        voice_url="https://old.example/voice", voice_method="GET",
        status_callback="https://old.example/status", status_callback_method="GET")
    application = SimpleNamespace(sid=APP_SID, account_sid=settings.account_sid,
        voice_url="https://old.example/native-agent", voice_method="GET")
    phone_resource, application_resource = Mock(), Mock()
    phone_resource.fetch.return_value = number
    application_resource.fetch.return_value = application
    phone_resource.update.side_effect = lambda **kw: [setattr(number, k, v) for k, v in kw.items()]
    application_resource.update.side_effect = lambda **kw: [setattr(application, k, v) for k, v in kw.items()]
    phone_numbers = Mock(return_value=phone_resource)
    phone_numbers.list.return_value = [number]
    applications = Mock(return_value=application_resource)
    client = SimpleNamespace(incoming_phone_numbers=phone_numbers, applications=applications)
    monkeypatch.setattr(configure_twilio, "ROOT", tmp_path)
    monkeypatch.setattr(configure_twilio, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(configure_twilio.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(configure_twilio, "Client", lambda *args, **kwargs: client)
    monkeypatch.setenv("TWILIO_NUMBER", settings.twilio_number)
    monkeypatch.delenv("TWILIO_API_KEY", raising=False)
    monkeypatch.delenv("TWILIO_API_SECRET", raising=False)
    monkeypatch.setattr(configure_twilio.sys, "argv", ["configure_twilio.py"])
    return SimpleNamespace(settings=settings, number=number, application=application,
        phone_resource=phone_resource, application_resource=application_resource,
        applications=applications, root=tmp_path)


@pytest.mark.parametrize("matches", [False, True])
def test_read_only_check_reports_native_application_match_without_mutation(configured, matches, capsys):
    h = configured
    if matches:
        h.application.voice_url = h.settings.public_base_url + "/twilio/native-agent"
        h.application.voice_method = "POST"
    configure_twilio.main()
    output = json.loads(capsys.readouterr().out)
    assert output["native_agent_matches"] is matches
    assert output["native_agent_application_sid"] == APP_SID
    assert output["native_agent_voice_url"] == h.application.voice_url
    h.applications.assert_called_once_with(APP_SID)
    h.phone_resource.update.assert_not_called()
    h.application_resource.update.assert_not_called()
    assert not (h.root / ".runtime").exists()


def test_apply_updates_reads_back_and_preserves_private_initial_application_backup(configured, monkeypatch, capsys):
    h = configured
    original_application = {"application_sid": APP_SID, "account_sid": h.settings.account_sid,
        "voice_url": h.application.voice_url, "voice_method": h.application.voice_method}
    monkeypatch.setattr(configure_twilio.sys, "argv", ["configure_twilio.py", "--apply"])
    configure_twilio.main()
    assert json.loads(capsys.readouterr().out)["native_agent_matches"] is True
    h.application_resource.update.assert_called_once_with(
        voice_url=h.settings.public_base_url + "/twilio/native-agent", voice_method="POST")
    assert h.application_resource.fetch.call_count == 2
    backup = h.root / ".runtime/twilio-before-native-application.json"
    assert json.loads(backup.read_text()) == original_application
    assert backup.stat().st_mode & 0o777 == 0o600
    number_backup = h.root / ".runtime/twilio-before.json"
    status_backup = h.root / ".runtime/twilio-before-status.json"
    saved = (backup.read_bytes(), number_backup.read_bytes(), status_backup.read_bytes())
    configure_twilio.main()
    assert (backup.read_bytes(), number_backup.read_bytes(), status_backup.read_bytes()) == saved


def test_apply_rejects_failed_application_readback(configured, monkeypatch, capsys):
    h = configured
    h.application_resource.update.side_effect = None
    monkeypatch.setattr(configure_twilio.sys, "argv", ["configure_twilio.py", "--apply"])
    with pytest.raises(ValueError, match="native-agent webhook"):
        configure_twilio.main()
    assert json.loads(capsys.readouterr().out)["native_agent_matches"] is False
    assert h.application_resource.fetch.call_count == 2


@pytest.mark.parametrize("field,value", [("account_sid", "AC" + "c" * 32), ("sid", "AP" + "d" * 32)])
def test_application_identity_mismatch_blocks_all_updates(configured, monkeypatch, field, value):
    h = configured
    setattr(h.application, field, value)
    monkeypatch.setattr(configure_twilio.sys, "argv", ["configure_twilio.py", "--apply"])
    with pytest.raises(ValueError, match="in this account"):
        configure_twilio.main()
    h.phone_resource.update.assert_not_called()
    h.application_resource.update.assert_not_called()
    assert not (h.root / ".runtime").exists()


def test_disabled_native_pilot_preserves_number_only_configuration(configured, monkeypatch, capsys):
    h = configured
    settings = replace(h.settings, native_conference_enabled=False)
    monkeypatch.setattr(configure_twilio.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(configure_twilio.sys, "argv", ["configure_twilio.py", "--apply"])
    configure_twilio.main()
    output = json.loads(capsys.readouterr().out)
    assert output["matches_local_tunnel"] is True
    assert "native_agent_matches" not in output
    h.applications.assert_not_called()
    assert not (h.root / ".runtime/twilio-before-native-application.json").exists()
