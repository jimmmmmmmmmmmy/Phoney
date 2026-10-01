"""Native browser App setup checks and saves routing without placing calls."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import configure_twilio
from test_switchboard import SETTINGS


BOT_APP = "AP" + "b" * 32
BROWSER_APP = "AP" + "c" * 32


@pytest.fixture
def configured(tmp_path, monkeypatch):
    settings = replace(SETTINGS, browser_voice_enabled=True, native_conference_enabled=False,
                       api_key="SK" + "d" * 32, api_secret="offline-api-secret-" * 3,
                       twilio_conference_app_sid=BOT_APP, twilio_browser_app_sid=BROWSER_APP)
    number = SimpleNamespace(sid="PN" + "1" * 32, phone_number=settings.twilio_number,
        capabilities={"voice": True}, voice_application_sid="", trunk_sid="",
        voice_url="https://old.example/voice", voice_method="GET",
        status_callback="https://old.example/status", status_callback_method="GET")
    bot = SimpleNamespace(sid=BOT_APP, account_sid=settings.account_sid,
                         voice_url="https://old.example/bot", voice_method="GET")
    browser = SimpleNamespace(sid=BROWSER_APP, account_sid=settings.account_sid,
        voice_url="https://old.example/browser", voice_method="GET",
        status_callback="https://old.example/browser-status", status_callback_method="GET")
    phone_resource = Mock()
    phone_resource.fetch.return_value = number
    phone_resource.update.side_effect = lambda **kw: [setattr(number, k, v) for k, v in kw.items()]
    resources = {}
    for app in (bot, browser):
        resource = Mock()
        resource.fetch.return_value = app
        resource.update.side_effect = lambda app=app, **kw: [setattr(app, k, v) for k, v in kw.items()]
        resources[app.sid] = resource
    phone_numbers = Mock(return_value=phone_resource)
    phone_numbers.list.return_value = [number]
    applications = Mock(side_effect=lambda sid: resources[sid])
    client = SimpleNamespace(incoming_phone_numbers=phone_numbers, applications=applications)
    monkeypatch.setattr(configure_twilio, "ROOT", tmp_path)
    monkeypatch.setattr(configure_twilio, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(configure_twilio.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(configure_twilio, "Client", lambda *args, **kwargs: client)
    monkeypatch.setenv("TWILIO_NUMBER", settings.twilio_number)
    monkeypatch.setenv("TWILIO_API_KEY", settings.api_key)
    monkeypatch.setenv("TWILIO_API_SECRET", settings.api_secret)
    monkeypatch.setattr(configure_twilio.sys, "argv", ["configure_twilio.py"])
    return SimpleNamespace(settings=settings, number=number, browser=browser, bot=bot,
        phone_resource=phone_resource, browser_resource=resources[BROWSER_APP],
        bot_resource=resources[BOT_APP], applications=applications, root=tmp_path)


def test_browser_only_activation_checks_both_apps_without_mutation(configured, capsys):
    h = configured
    configure_twilio.main()
    output = json.loads(capsys.readouterr().out)
    assert output["browser_voice_matches"] is False
    assert output["native_agent_matches"] is False
    assert {call.args[0] for call in h.applications.call_args_list} == {BOT_APP, BROWSER_APP}
    for resource in (h.phone_resource, h.browser_resource, h.bot_resource):
        resource.update.assert_not_called()
    assert not (h.root / ".runtime").exists()
    assert h.settings.api_secret not in json.dumps(output)


def test_apply_syncs_voice_and_terminal_callback_preserving_private_originals(configured, monkeypatch, capsys):
    h = configured
    original = {"application_sid": BROWSER_APP, "account_sid": h.settings.account_sid,
        "voice_url": h.browser.voice_url, "voice_method": h.browser.voice_method,
        "status_callback": h.browser.status_callback, "status_callback_method": h.browser.status_callback_method}
    monkeypatch.setattr(configure_twilio.sys, "argv", ["configure_twilio.py", "--apply"])
    configure_twilio.main()
    output = json.loads(capsys.readouterr().out)
    assert output["browser_voice_matches"] and output["native_agent_matches"]
    h.browser_resource.update.assert_called_once_with(
        voice_url=h.settings.public_base_url + "/twilio/browser-voice", voice_method="POST",
        status_callback=h.settings.public_base_url + "/twilio/browser-status", status_callback_method="POST")
    h.bot_resource.update.assert_called_once_with(
        voice_url=h.settings.public_base_url + "/twilio/native-agent", voice_method="POST")
    assert h.browser_resource.fetch.call_count == 2 and h.bot_resource.fetch.call_count == 2
    backup = h.root / ".runtime/twilio-before-browser-application.json"
    assert json.loads(backup.read_text()) == original
    assert backup.stat().st_mode & 0o777 == 0o600
    saved = backup.read_bytes()
    configure_twilio.main()
    assert backup.read_bytes() == saved


def test_status_callback_mismatch_fails_readback_even_when_voice_url_matches(configured, monkeypatch, capsys):
    h = configured
    h.browser_resource.update.side_effect = lambda **kw: [setattr(h.browser, k, v) for k, v in kw.items()
                                                         if k != "status_callback"]
    monkeypatch.setattr(configure_twilio.sys, "argv", ["configure_twilio.py", "--apply"])
    with pytest.raises(ValueError, match="browser-voice webhooks"):
        configure_twilio.main()
    assert json.loads(capsys.readouterr().out)["browser_voice_matches"] is False


@pytest.mark.parametrize("field,value", [("account_sid", "AC" + "e" * 32), ("sid", "AP" + "f" * 32)])
def test_wrong_browser_application_blocks_number_and_bot_mutation(configured, monkeypatch, field, value):
    h = configured
    setattr(h.browser, field, value)
    monkeypatch.setattr(configure_twilio.sys, "argv", ["configure_twilio.py", "--apply"])
    with pytest.raises(ValueError, match="browser-voice TwiML App in this account"):
        configure_twilio.main()
    for resource in (h.phone_resource, h.browser_resource, h.bot_resource):
        resource.update.assert_not_called()
    assert not (h.root / ".runtime").exists()
