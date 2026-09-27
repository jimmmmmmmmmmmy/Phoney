#!/usr/bin/env python3
"""Backfill known callers or save an authored summary; never places or changes calls."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from call_details import CallDetailsStore
from config import Settings
from media_capture.playback import RecordingLibrary
from transcription import storage
from voicemail import VoicemailStore


def local_sessions(settings):
    return storage.load(settings.transcript_storage_dir) if settings.transcript_storage_dir else []


def local_session(settings, call_sid):
    """Read a specific saved call, including calls outside the recent list."""
    return storage.load_call(settings.transcript_storage_dir, call_sid) if settings.transcript_storage_dir else None


def backfill(settings, store, client):
    """Fetch only callers already represented in the bounded local history."""
    sids = {item["call_sid"] for item in local_sessions(settings)}
    sids.update(item["call_sid"] for item in RecordingLibrary(settings).snapshot()["recordings"])
    sids.update(item["call_sid"] for item in VoicemailStore(settings).snapshot()["voicemails"])
    count = 0
    for sid in sorted(sids):
        record = client.calls(sid).fetch()
        if (record.sid != sid or record.account_sid != settings.account_sid
                or record.direction != "inbound" or record.to != settings.twilio_number):
            continue
        started = record.start_time.isoformat() if record.start_time else None
        if not store.start(sid, record._from, started_at=started):
            raise ValueError("Caller details could not be saved")
        if record.end_time:
            duration = int(record.duration) if str(record.duration).isdigit() else None
            if not store.finish(sid, ended_at=record.end_time.isoformat(), duration_seconds=duration):
                raise ValueError("Call duration could not be saved")
        count += 1
    return count


def save_summary(settings, store, sid, text_file):
    session = local_session(settings, sid)
    if not session or not session.get("ended_at") or not session.get("segments"):
        raise ValueError("Choose a completed local transcript with finalized text")
    # Bound input before decoding; the store enforces the summary character limit.
    with Path(text_file).open("rb") as source:
        content = source.read(16001)
    if len(content) > 16000:
        raise ValueError("Summary file is too large")
    text = content.decode("utf-8").strip()
    if not store.start(sid, "", started_at=session["started_at"]):
        raise ValueError("Call details could not be saved")
    if not store.finish(sid, ended_at=session["ended_at"]):
        raise ValueError("Call completion could not be saved")
    if not store.set_summary(sid, text, session):
        raise ValueError("Summary could not be saved; check text length and storage")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Private environment of the running server")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backfill", help="Read caller numbers and durations for known local calls from Twilio")
    summary = commands.add_parser("summarize", help="Save a summary already written by an operator or agent")
    summary.add_argument("call_sid")
    summary.add_argument("--text-file", type=Path, required=True)
    retry = commands.add_parser("retry-summary", help="Retry a failed automatic summary after fixing billing/configuration")
    retry.add_argument("call_sid")
    retry.add_argument("--kind", choices=("detailed", "brief"), default="detailed",
                       help="Summary job to retry; a completed counterpart is preserved")
    args = parser.parse_args()
    load_dotenv(args.env_file or ROOT / ".env", override=bool(args.env_file))
    settings = Settings.from_env()
    if not settings.call_details_storage_dir:
        parser.error("Set CALL_DETAILS_STORAGE_DIR to an absolute private directory")
    store = CallDetailsStore(settings.call_details_storage_dir)
    if args.command == "backfill":
        client = Client(settings.api_key or settings.account_sid,
                        settings.api_secret if settings.api_key else settings.auth_token,
                        account_sid=settings.account_sid,
                        http_client=TwilioHttpClient(timeout=15, max_retries=0))
        print(f"Saved caller details for {backfill(settings, store, client)} local calls.")
    elif args.command == "summarize":
        save_summary(settings, store, args.call_sid, args.text_file)
        print("Saved summary for the completed transcript.")
    else:
        document = local_session(settings, args.call_sid)
        if not document or not store.retry_summary(args.call_sid, document, kind=args.kind):
            raise ValueError("Choose a failed automatic summary for an ended local transcript")
        print(f"Failed {args.kind} summary reset; the running summary worker will retry it.")


if __name__ == "__main__":
    try:
        main()
    except TwilioRestException as exc:
        print(f"Twilio lookup failed: HTTP {exc.status}, code {exc.code}.", file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, UnicodeError):
        print("Call details update failed. Check the private paths, completed transcript, and summary text.",
              file=sys.stderr)
        sys.exit(1)
