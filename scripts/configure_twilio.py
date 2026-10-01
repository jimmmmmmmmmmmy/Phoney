"""Check or set number and enabled native voice App webhooks. Does not place calls."""

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from config import Settings


def _application_backup(path, application, *, status=False):
    """Preserve the initial App routing privately across later URL changes."""
    fields = {"application_sid": application.sid, "account_sid": application.account_sid,
              "voice_url": application.voice_url, "voice_method": application.voice_method}
    if status:
        fields.update(status_callback=application.status_callback,
                      status_callback_method=application.status_callback_method)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w") as backup_file:
        json.dump(fields, backup_file, indent=2)
        backup_file.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Save enabled Twilio webhook URLs as HTTP POST")
    parser.add_argument("--env-file", type=Path, help="Read the installed service's environment")
    args = parser.parse_args()
    load_dotenv(args.env_file or ROOT / ".env", override=bool(args.env_file))
    settings = Settings.from_env()
    number = os.getenv("TWILIO_NUMBER", "")
    if not number:
        raise ValueError("Set TWILIO_NUMBER to the existing number in .env.")
    key, secret = os.getenv("TWILIO_API_KEY"), os.getenv("TWILIO_API_SECRET")
    if bool(key) != bool(secret):
        raise ValueError("Supply TWILIO_API_KEY and TWILIO_API_SECRET together, or leave both blank.")
    client = Client(key or settings.account_sid, secret if key else settings.auth_token,
                    account_sid=settings.account_sid, http_client=TwilioHttpClient(timeout=15))
    matches = client.incoming_phone_numbers.list(phone_number=number, limit=2)
    if len(matches) != 1 or not matches[0].capabilities.get("voice"):
        raise ValueError("Expected one existing Voice-enabled number in this account.")
    current = matches[0]
    if current.voice_application_sid or current.trunk_sid:
        raise ValueError("Number uses a TwiML App or SIP trunk. Inspect its routing before changing it.")
    expected = settings.public_base_url + "/voice"
    expected_status = settings.public_base_url + "/status"
    expected_agent = settings.public_base_url + "/twilio/native-agent"
    expected_browser = settings.public_base_url + "/twilio/browser-voice"
    expected_browser_status = settings.public_base_url + "/twilio/browser-status"
    application = None
    application_sid = getattr(settings, "twilio_conference_app_sid", "")
    browser_enabled = getattr(settings, "browser_voice_enabled", False)
    if (getattr(settings, "native_conference_enabled", False) or browser_enabled) and application_sid:
        application = client.applications(application_sid).fetch()
        if application.sid != application_sid or application.account_sid != settings.account_sid:
            raise ValueError("Expected the configured native-agent TwiML App in this account.")
    browser_application = None
    browser_application_sid = getattr(settings, "twilio_browser_app_sid", "")
    if browser_enabled and browser_application_sid:
        browser_application = client.applications(browser_application_sid).fetch()
        if (browser_application.sid != browser_application_sid
                or browser_application.account_sid != settings.account_sid):
            raise ValueError("Expected the configured browser-voice TwiML App in this account.")
    if args.apply:
        runtime = ROOT / ".runtime"
        runtime.mkdir(mode=0o700, exist_ok=True)
        backup = runtime / "twilio-before.json"
        if not backup.exists():
            backup.write_text(json.dumps({"number_sid": current.sid, "voice_url": current.voice_url,
                                           "voice_method": current.voice_method}, indent=2) + "\n")
            backup.chmod(0o600)
        status_backup = runtime / "twilio-before-status.json"
        if not status_backup.exists():
            status_backup.write_text(json.dumps({"number_sid": current.sid,
                "status_callback": current.status_callback,
                "status_callback_method": current.status_callback_method}, indent=2) + "\n")
            status_backup.chmod(0o600)
        if application is not None:
            _application_backup(runtime / "twilio-before-native-application.json", application)
        if browser_application is not None:
            _application_backup(runtime / "twilio-before-browser-application.json",
                                browser_application, status=True)
        client.incoming_phone_numbers(current.sid).update(
            voice_url=expected, voice_method="POST",
            status_callback=expected_status, status_callback_method="POST")
        current = client.incoming_phone_numbers(current.sid).fetch()
        if application is not None:
            client.applications(application_sid).update(voice_url=expected_agent, voice_method="POST")
            application = client.applications(application_sid).fetch()
        if browser_application is not None:
            client.applications(browser_application_sid).update(voice_url=expected_browser, voice_method="POST",
                status_callback=expected_browser_status, status_callback_method="POST")
            browser_application = client.applications(browser_application_sid).fetch()
    output = {"phone_number": current.phone_number, "voice_url": current.voice_url,
                      "voice_method": current.voice_method,
                      "status_callback": current.status_callback,
                      "status_callback_method": current.status_callback_method,
                      "matches_local_tunnel": current.voice_url == expected and current.voice_method == "POST"
                          and current.status_callback == expected_status and current.status_callback_method == "POST"}
    if application is not None:
        output.update(native_agent_application_sid=application.sid,
            native_agent_voice_url=application.voice_url, native_agent_voice_method=application.voice_method,
            native_agent_matches=(application.sid == application_sid and application.account_sid == settings.account_sid
                and application.voice_url == expected_agent and application.voice_method == "POST"))
    if browser_application is not None:
        output.update(browser_voice_application_sid=browser_application.sid,
            browser_voice_url=browser_application.voice_url, browser_voice_method=browser_application.voice_method,
            browser_status_callback=browser_application.status_callback,
            browser_status_callback_method=browser_application.status_callback_method,
            browser_voice_matches=(browser_application.sid == browser_application_sid
                and browser_application.account_sid == settings.account_sid
                and browser_application.voice_url == expected_browser and browser_application.voice_method == "POST"
                and browser_application.status_callback == expected_browser_status
                and browser_application.status_callback_method == "POST"))
    print(json.dumps(output, indent=2))
    if args.apply and (current.voice_url != expected or current.voice_method != "POST"
                      or current.status_callback != expected_status or current.status_callback_method != "POST"):
        raise ValueError("Twilio read-back did not match the requested webhook.")
    if args.apply and application is not None and not output["native_agent_matches"]:
        raise ValueError("Twilio read-back did not match the requested native-agent webhook.")
    if args.apply and browser_application is not None and not output["browser_voice_matches"]:
        raise ValueError("Twilio read-back did not match the requested browser-voice webhooks.")


if __name__ == "__main__":
    try:
        main()
    except TwilioRestException as exc:
        print(f"Twilio API failed: HTTP {exc.status}, code {exc.code}. Check account credentials and number access.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
