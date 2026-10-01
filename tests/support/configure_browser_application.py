"""Shared fixtures and fakes for focused integration checks."""

from dataclasses import replace


import json


from types import SimpleNamespace


from unittest.mock import Mock


import pytest


from scripts import configure_twilio


from support.switchboard import SETTINGS


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
