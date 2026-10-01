"""Shared fixtures and fakes for focused integration checks."""

from datetime import datetime, timedelta, timezone


from fastapi import FastAPI


from fastapi.testclient import TestClient


from call_details import CallDetailsStore


from call_history import PAGE_SIZE


from dashboard import register_dashboard


from transcription import TranscriptionManager


from transcription import storage


from support.transcription import settings


from support.call_details import session, NUMBER


from support.recording_playback import library, write_capture


def sid(number):
    return "CA" + f"{number:032x}"


def document(number, *, minute=None):
    result = session(sid(number))
    start = datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=number if minute is None else minute)
    result.update(schema_version=1, provider="deepgram", model="nova-3", finish_reason="", storage_error="",
                  started_at=start.isoformat(), ended_at=(start + timedelta(seconds=30)).isoformat())
    result["segments"][0]["text"] = f"Saved conversation {number}."
    result["tracks"] = {name: {"meaning": meaning, "status": "completed", "error": "", "interim": ""}
                        for name, meaning in (("inbound", "caller-input"), ("outbound", "caller-playback"))}
    return result


def seed(root, count=45):
    store = CallDetailsStore(str(root / "details"))
    for number in range(1, count + 1):
        doc = document(number)
        storage.save(str(root / "transcripts"), doc)
        assert store.start(doc["call_sid"], NUMBER, doc["started_at"])
        assert store.finish(doc["call_sid"], doc["ended_at"], 30)
        assert store.set_summary(doc["call_sid"], f"Summary {number}", doc)
        assert store.set_summary(doc["call_sid"], f"Brief {number}", doc, kind="brief")


def viewer(root):
    manager = TranscriptionManager(settings(root / "transcripts"))
    details = CallDetailsStore(str(root / "details"))
    recordings = library(root / "recordings")
    app = FastAPI()
    register_dashboard(app, manager.settings, manager, recording_library=recordings, call_details_store=details)
    return TestClient(app), manager, details, recordings
