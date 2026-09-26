"""Check or set voice and caller-status webhooks. Does not place calls."""

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Save voice and caller-status URLs as HTTP POST")
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
        client.incoming_phone_numbers(current.sid).update(
            voice_url=expected, voice_method="POST",
            status_callback=expected_status, status_callback_method="POST")
        current = client.incoming_phone_numbers(current.sid).fetch()
    print(json.dumps({"phone_number": current.phone_number, "voice_url": current.voice_url,
                      "voice_method": current.voice_method,
                      "status_callback": current.status_callback,
                      "status_callback_method": current.status_callback_method,
                      "matches_local_tunnel": current.voice_url == expected and current.voice_method == "POST"
                          and current.status_callback == expected_status and current.status_callback_method == "POST"}, indent=2))
    if args.apply and (current.voice_url != expected or current.voice_method != "POST"
                      or current.status_callback != expected_status or current.status_callback_method != "POST"):
        raise ValueError("Twilio read-back did not match the requested webhook.")


if __name__ == "__main__":
    try:
        main()
    except TwilioRestException as exc:
        print(f"Twilio API failed: HTTP {exc.status}, code {exc.code}. Check account credentials and number access.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
