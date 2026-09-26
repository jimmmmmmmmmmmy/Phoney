"""Caller metadata enters through signed callbacks; summaries remain local writes."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from twilio.request_validator import RequestValidator

from app import create_app
from call_details import CallDetailsStore
from config import Settings
from scripts import call_details as commands

SID = "CA" + "2" * 32
SETTINGS = Settings("AC" + "1" * 32, "test-secret", "https://operator.example",
                    twilio_number="+15555550100", callee_number="+15555550200")
FORM = {"AccountSid": SETTINGS.account_sid, "CallSid": SID,
        "From": "+15555550300", "To": SETTINGS.twilio_number, "Direction": "inbound"}
SESSION = {"call_sid": SID, "started_at": "2026-09-26T12:00:00+00:00",
           "ended_at": "2026-09-26T12:01:00+00:00", "status": "completed", "tracks": {},
           "segments": [{"id": "1", "track": "inbound", "start_ms": 0, "end_ms": 1000,
                         "text": "Please call tomorrow.", "confidence": .9}]}


class Gateway:
    async def end_call(self, sid):
        pass

    async def end_conference(self, sid):
        pass


def signed(client, path, form):
    signature = RequestValidator(SETTINGS.auth_token).compute_signature(SETTINGS.public_base_url + path, form)
    return client.post(path, data=form, headers={"X-Twilio-Signature": signature})


def test_signed_caller_and_final_duration_survive_restart(tmp_path):
    settings = replace(SETTINGS, call_details_storage_dir=str(tmp_path / "details"))
    with TestClient(create_app(settings, gateway=Gateway())) as client:
        assert client.post("/voice", data=FORM).status_code == 403
        assert client.get("/api/transcripts").json()["call_details"]["calls"] == []
        assert signed(client, "/voice", FORM).status_code == 200
        assert signed(client, "/status", {**FORM, "CallStatus": "completed", "CallDuration": "61"}).status_code == 204
        record = client.get("/api/transcripts").json()["call_details"]["calls"][0]
        assert record["caller_number"] == FORM["From"] and record["duration_seconds"] == 61
        assert record["ended_at"]
        assert client.post("/api/transcripts", json={"caller_number": "changed"}).status_code == 405
    restored = CallDetailsStore(settings.call_details_storage_dir).snapshot()["calls"][0]
    assert restored == record


def test_summary_uses_full_selected_transcript_before_other_sessions_are_trimmed(tmp_path):
    settings = replace(SETTINGS, call_details_storage_dir=str(tmp_path / "details"))
    app = create_app(settings, gateway=Gateway())
    store = app.state.call_details
    store.start(SID, FORM["From"], started_at=SESSION["started_at"])
    store.finish(SID, ended_at=SESSION["ended_at"])
    assert store.set_summary(SID, "The caller requested a call tomorrow.", SESSION)
    other = {**deepcopy(SESSION), "call_sid": "CA" + "3" * 32}
    app.state.transcription.snapshot = lambda: {"sessions": deepcopy([other, SESSION]), "enabled": True}
    with TestClient(app) as client:
        data = client.get("/api/transcripts").json()
        assert data["sessions"][1]["segments"] == []
        assert data["call_details"]["calls"][0]["summary"]["text"] == "The caller requested a call tomorrow."
        exported = client.get(f"/api/transcripts/{SID}/export?format=json").json()
        assert exported["call_details"]["caller_number"] == FORM["From"]
        assert exported["call_details"]["summary"]["source"] == "agent"


@pytest.mark.parametrize("duration", ["-1", "nan", "1.5", "9" * 1000, "١٢"])
def test_malformed_duration_does_not_break_call_cleanup(tmp_path, duration):
    settings = replace(SETTINGS, call_details_storage_dir=str(tmp_path / "details"))
    with TestClient(create_app(settings, gateway=Gateway())) as client:
        assert signed(client, "/voice", FORM).status_code == 200
        assert signed(client, "/status", {**FORM, "CallStatus": "completed", "CallDuration": duration}).status_code == 204
        assert client.get("/api/transcripts").json()["call_details"]["calls"][0]["ended_at"]


def test_backfill_only_fetches_known_inbound_calls(tmp_path, monkeypatch):
    settings = replace(SETTINGS, call_details_storage_dir=str(tmp_path / "details"))
    monkeypatch.setattr(commands, "local_sessions", lambda settings: [deepcopy(SESSION)])
    record = SimpleNamespace(sid=SID, account_sid=SETTINGS.account_sid, direction="inbound",
                             to=SETTINGS.twilio_number, _from=FORM["From"],
                             start_time=datetime(2026, 9, 26, 12, tzinfo=timezone.utc),
                             end_time=datetime(2026, 9, 26, 12, 1, tzinfo=timezone.utc), duration="60")
    calls = []

    class Client:
        def calls(self, sid):
            calls.append(sid)
            return SimpleNamespace(fetch=lambda: record)

    store = CallDetailsStore(settings.call_details_storage_dir)
    assert commands.backfill(settings, store, Client()) == 1
    assert calls == [SID]
    assert store.snapshot()["calls"][0]["duration_seconds"] == 60
    record.direction = "outbound-api"
    assert commands.backfill(settings, store, Client()) == 0


def test_cli_saves_authored_text_and_keeps_existing_caller(tmp_path, monkeypatch):
    settings = replace(SETTINGS, call_details_storage_dir=str(tmp_path / "details"))
    monkeypatch.setattr(commands, "local_sessions", lambda settings: [deepcopy(SESSION)])
    store = CallDetailsStore(settings.call_details_storage_dir)
    store.start(SID, FORM["From"], started_at=SESSION["started_at"])
    store.finish(SID, ended_at=SESSION["ended_at"], duration_seconds=60)
    text_file = tmp_path / "summary.txt"
    text_file.write_text("The caller requested a call tomorrow.\n")
    commands.save_summary(settings, store, SID, text_file)
    record = store.snapshot([SESSION])["calls"][0]
    assert record["caller_number"] == FORM["From"]
    assert record["duration_seconds"] == 60
    assert record["summary"]["text"] == "The caller requested a call tomorrow."


@pytest.mark.parametrize("kind", ["detailed", "brief"])
def test_cli_retry_resets_only_the_selected_failed_job(tmp_path, monkeypatch, kind):
    settings = replace(SETTINGS, call_details_storage_dir=str(tmp_path / "details"))
    store = CallDetailsStore(settings.call_details_storage_dir)
    other = "brief" if kind == "detailed" else "detailed"
    assert store.set_summary(SID, "Saved counterpart.", SESSION, kind=other)
    assert store.begin_summary(SID, SESSION, kind=kind)
    assert store.fail_summary(SID, SESSION, "billing_required", kind=kind)
    monkeypatch.setattr(commands, "local_sessions", lambda settings: [deepcopy(SESSION)])
    monkeypatch.setattr(commands, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(commands, "Settings", SimpleNamespace(from_env=lambda: settings))
    arguments = ["call_details.py", "retry-summary", SID]
    if kind == "brief":
        arguments.extend(["--kind", "brief"])
    monkeypatch.setattr(commands.sys, "argv", arguments)
    commands.main()
    reloaded = CallDetailsStore(settings.call_details_storage_dir)
    assert reloaded.summary_state(SID, SESSION, kind=kind)["status"] == "missing"
    assert reloaded.summary_state(SID, SESSION, kind=other)["status"] == "completed"
    other_key = "summary" if other == "detailed" else "brief_summary"
    assert reloaded.snapshot([SESSION])["calls"][0][other_key]["text"] == "Saved counterpart."


def test_relative_storage_configuration_is_rejected():
    with pytest.raises(ValueError, match="CALL_DETAILS_STORAGE_DIR"):
        replace(SETTINGS, call_details_storage_dir="relative-path")


@pytest.mark.parametrize("field", ["transcript_storage_dir", "voicemail_storage_dir"])
def test_details_cannot_overwrite_other_json_stores(tmp_path, field):
    with pytest.raises(ValueError, match="must differ"):
        replace(SETTINGS, call_details_storage_dir=str(tmp_path / "calls"),
                **{field: str(tmp_path / "unused" / ".." / "calls")})
