"""Focused product and boundary checks; test helpers live in support."""

import json

import pytest

from scripts import configure_twilio

from support.configure_browser_application import BROWSER_APP, configured


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
