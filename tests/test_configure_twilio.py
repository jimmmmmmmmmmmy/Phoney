"""Configuration includes parent-call termination callbacks, without placing calls."""

from types import SimpleNamespace
from unittest.mock import Mock

from scripts import configure_twilio
from test_switchboard import SETTINGS


def test_voice_and_status_webhooks_are_applied_and_read_back(tmp_path, monkeypatch, capsys):
    current = SimpleNamespace(sid="PN" + "1" * 32, phone_number=SETTINGS.twilio_number,
        capabilities={"voice": True}, voice_application_sid="", trunk_sid="",
        voice_url="https://old.example/voice", voice_method="POST",
        status_callback="", status_callback_method="POST")
    number = Mock()
    number.update.side_effect = lambda **kw: [setattr(current, key, value) for key, value in kw.items()]
    number.fetch.return_value = current
    collection = Mock(return_value=number)
    collection.list.return_value = [current]
    client = SimpleNamespace(incoming_phone_numbers=collection)
    monkeypatch.setattr(configure_twilio, "ROOT", tmp_path)
    monkeypatch.setattr(configure_twilio, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setattr(configure_twilio.Settings, "from_env", lambda: SETTINGS)
    monkeypatch.setattr(configure_twilio, "Client", lambda *a, **kw: client)
    monkeypatch.setenv("TWILIO_NUMBER", SETTINGS.twilio_number)
    monkeypatch.delenv("TWILIO_API_KEY", raising=False)
    monkeypatch.delenv("TWILIO_API_SECRET", raising=False)
    monkeypatch.setattr("sys.argv", ["configure_twilio.py", "--apply"])
    configure_twilio.main()
    number.update.assert_called_once_with(voice_url=SETTINGS.public_base_url + "/voice",
        voice_method="POST", status_callback=SETTINGS.public_base_url + "/status",
        status_callback_method="POST")
    assert '"matches_local_tunnel": true' in capsys.readouterr().out
    assert (tmp_path / ".runtime/twilio-before-status.json").stat().st_mode & 0o777 == 0o600
