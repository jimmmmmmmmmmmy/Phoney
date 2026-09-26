"""Unanswered-call recording callbacks with fake telephony; no real phone calls."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app import create_app
from voicemail import VoicemailStore
from test_switchboard import FakeGateway, PARENT, OUTBOUND, SETTINGS, conference_form, call_form
from test_media_webhooks import (signed_post, stream_and_token, socket_headers, send_start,
                                 send_audio, read_manifest, assert_socket_closed)

RECORDING = "RE" + "a" * 32


class Gateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.redirects = []

    async def redirect_call(self, sid, url):
        self.redirects.append((sid, url))


def settings_for(tmp_path, **changes):
    return replace(SETTINGS, voicemail_enabled=True,
                   voicemail_storage_dir=str(tmp_path / "voicemails"),
                   media_capture_enabled=True, media_storage_dir=str(tmp_path / "audio"),
                   deploy_control_token="test-deploy-" * 4, **changes)


def post(client, settings, path, **fields):
    return signed_post(client, settings, path,
                       {"AccountSid": settings.account_sid, "CallSid": PARENT, **fields})


def unanswered(client, settings):
    assert signed_post(client, settings, f"/conference/events/{PARENT}", conference_form()).status_code == 204
    client.portal.call(client.app.state.switchboard.wait_idle)
    assert signed_post(client, settings, f"/calls/status/{PARENT}", call_form("no-answer")).status_code == 204
    client.portal.call(client.app.state.switchboard.wait_idle)


def recording(client, settings, status="completed", **changes):
    values = dict(RecordingSid=RECORDING, RecordingStatus=status,
                  RecordingDuration="9", RecordingSource="RecordVerb",
                  RecordingUrl="https://api.twilio.com/private-recording-fixture")
    values.update(changes)
    return post(client, settings, f"/voicemail/recording/{PARENT}", **values)


def test_unanswered_call_keeps_media_alive_through_voicemail_and_finalizes(tmp_path):
    settings, gateway = settings_for(tmp_path), Gateway()
    with TestClient(create_app(settings, gateway=gateway)) as client:
        _, token = stream_and_token(client, settings)
        with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)) as ws:
            send_start(ws, settings, token)
            send_audio(ws, sample=0)
            unanswered(client, settings)
            assert gateway.redirects == [(PARENT, settings.public_base_url + f"/voicemail/{PARENT}")]
            assert PARENT not in gateway.ended_calls
            assert client.app.state.media_capture.active_count == 1
            stale = post(client, settings, f"/conference/finished/{PARENT}")
            assert ET.fromstring(stale.text).find("Redirect").text.endswith(f"/voicemail/{PARENT}")
            xml = ET.fromstring(post(client, settings, f"/voicemail/{PARENT}").text)
            assert "name, callback number, and message" in xml.find("Say").text
            record = xml.find("Record")
            assert record.attrib == {
                "action": settings.public_base_url + f"/voicemail/finished/{PARENT}",
                "method": "POST", "maxLength": "120", "timeout": "5", "finishOnKey": "#",
                "playBeep": "true", "trim": "do-not-trim", "transcribe": "false",
                "recordingStatusCallback": settings.public_base_url + f"/voicemail/recording/{PARENT}",
                "recordingStatusCallbackMethod": "POST",
                "recordingStatusCallbackEvent": "in-progress completed absent"}
            assert xml.find("Start") is None  # Preserve the existing stream; do not duplicate it.
            assert recording(client, settings, "in-progress", RecordingDuration="").status_code == 204
            send_audio(ws, timestamp=20, chunk=2, sample=128)
            receipt = client.get("/api/voicemails").json()["voicemails"][0]
            assert receipt["recording_status"] == "recording" and receipt["reason"] == "no-answer"
            finished = post(client, settings, f"/voicemail/finished/{PARENT}", Digits="#")
            assert ET.fromstring(finished.text).find("Hangup") is not None
            client.portal.call(client.app.state.switchboard.wait_idle)
            assert assert_socket_closed(ws) == 1000
        receipt = client.get("/api/voicemails").json()["voicemails"][0]
        assert receipt["recording_status"] == "processing"  # File availability is not yet confirmed.
        assert recording(client, settings).status_code == 204
        receipt = client.get("/api/voicemails").json()["voicemails"][0]
        assert receipt["recording_status"] == "completed" and receipt["duration_seconds"] == 9
        assert client.app.state.switchboard.active_count == 0
        assert PARENT in gateway.ended_calls
        assert read_manifest(settings)["tracks"]["inbound"]["frames"] == 2
        assert "RecordingUrl" not in json.dumps(receipt) and "api.twilio.com" not in json.dumps(receipt)
        assert client.get("/internal/deploy").status_code == 403


@pytest.mark.parametrize("path", ["/voicemail/", "/voicemail/finished/", "/voicemail/recording/"])
def test_voicemail_callbacks_require_signature_and_exact_parent(tmp_path, path):
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings, gateway=Gateway())) as client:
        assert client.post(path + PARENT, data={"AccountSid": settings.account_sid, "CallSid": PARENT}).status_code == 403
        assert post(client, settings, path + PARENT, CallSid=OUTBOUND).status_code == 400
        assert client.get(path + PARENT).status_code == 405


def test_no_public_entry_to_voicemail_and_no_redirect_after_answer(tmp_path):
    settings, gateway = settings_for(tmp_path), Gateway()
    with TestClient(create_app(settings, gateway=gateway)) as client:
        xml = ET.fromstring(post(client, settings, f"/voicemail/{PARENT}").text)
        assert xml.find("Hangup") is not None and xml.find("Record") is None
        stream_and_token(client, settings)
        signed_post(client, settings, f"/conference/events/{PARENT}", conference_form())
        client.portal.call(client.app.state.switchboard.wait_idle)
        signed_post(client, settings, f"/calls/status/{PARENT}", call_form("in-progress"))
        signed_post(client, settings, f"/calls/status/{PARENT}", call_form("completed"))
        client.portal.call(client.app.state.switchboard.wait_idle)
        assert gateway.redirects == []
        assert client.get("/api/voicemails").json()["voicemails"] == []


def test_metadata_survives_restart_and_late_callback_updates_without_session(tmp_path):
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings, gateway=Gateway())) as client:
        stream_and_token(client, settings)
        unanswered(client, settings)
        post(client, settings, f"/voicemail/{PARENT}")
        post(client, settings, f"/voicemail/finished/{PARENT}", Digits="hangup")
    saved = Path(settings.voicemail_storage_dir) / f"{PARENT}.json"
    assert json.loads(saved.read_text())["recording_status"] == "processing"
    assert saved.stat().st_mode & 0o777 == 0o600
    with TestClient(create_app(settings, gateway=Gateway())) as client:
        assert recording(client, settings).status_code == 204
        assert recording(client, settings, "in-progress").status_code == 204
        assert recording(client, settings, RecordingSid="RE" + "b" * 32).status_code == 400
        assert recording(client, settings, RecordingDuration="9999").status_code == 400
        receipt = client.get("/api/voicemails").json()["voicemails"][0]
        assert receipt["recording_status"] == "completed"
        assert client.app.state.switchboard.active_count == 0


@pytest.mark.parametrize("status", ["absent", "failed"])
def test_missing_recording_is_not_reported_as_saved(tmp_path, status):
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings, gateway=Gateway())) as client:
        stream_and_token(client, settings)
        unanswered(client, settings)
        post(client, settings, f"/voicemail/{PARENT}")
        assert recording(client, settings, status).status_code == 204
        client.portal.call(client.app.state.switchboard.wait_idle)
        assert client.get("/api/voicemails").json()["voicemails"][0]["recording_status"] == status
        assert client.app.state.switchboard.active_count == 0


def test_voicemail_disk_writer_counts_as_pending_and_reports_failures(tmp_path, monkeypatch):
    from voicemail import storage
    async def run():
        store = VoicemailStore(settings_for(tmp_path))
        def fail(*args):
            raise OSError("private-path-must-not-leak")
        monkeypatch.setattr(storage, "save", fail)
        assert store.start(PARENT, "no-answer")
        assert store.active_count == 1
        await store.close()
        snapshot = store.snapshot()
        assert snapshot["voicemails"][0]["storage_error"] == "save-failed"
        assert "private-path" not in json.dumps(snapshot)
        assert store.active_count == 0
    asyncio.run(run())


def test_caller_hangup_during_greeting_does_not_wait_for_record_callback(tmp_path):
    settings, gateway = settings_for(tmp_path), Gateway()
    with TestClient(create_app(settings, gateway=gateway)) as client:
        stream_and_token(client, settings)
        unanswered(client, settings)
        post(client, settings, f"/voicemail/{PARENT}")
        assert post(client, settings, "/status", CallStatus="completed").status_code == 204
        client.portal.call(client.app.state.switchboard.wait_idle)
        assert client.app.state.switchboard.active_count == 0
        receipt = client.get("/api/voicemails").json()["voicemails"][0]
        assert receipt["ended_at"] and receipt["recording_sid"] == ""
        assert receipt["recording_status"] != "completed"


def test_caller_status_before_fallback_prevents_later_voicemail_redirect(tmp_path):
    settings, gateway = settings_for(tmp_path), Gateway()
    with TestClient(create_app(settings, gateway=gateway)) as client:
        stream_and_token(client, settings)
        signed_post(client, settings, f"/conference/events/{PARENT}", conference_form())
        client.portal.call(client.app.state.switchboard.wait_idle)
        assert post(client, settings, "/status", CallStatus="completed").status_code == 204
        signed_post(client, settings, f"/calls/status/{PARENT}", call_form("no-answer"))
        client.portal.call(client.app.state.switchboard.wait_idle)
        assert client.app.state.switchboard.active_count == 0
        assert gateway.redirects == []


def test_late_callback_can_load_receipt_outside_recent_ten(tmp_path):
    settings = settings_for(tmp_path)
    async def seed():
        store = VoicemailStore(settings)
        store.start(PARENT, "no-answer")
        store.finish(PARENT)
        for n in range(16):
            sid = "CA" + f"{n:032x}"
            store.start(sid, "no-answer")
            store.finish(sid)
        await store.close()
    asyncio.run(seed())
    with TestClient(create_app(settings, gateway=Gateway())) as client:
        assert PARENT not in client.app.state.voicemails.records
        assert recording(client, settings).status_code == 204
        assert client.app.state.voicemails.records[PARENT]["recording_status"] == "completed"
    saved = Path(settings.voicemail_storage_dir) / f"{PARENT}.json"
    assert json.loads(saved.read_text())["recording_status"] == "completed"


def test_late_callback_does_not_read_symlink_or_malformed_receipts(tmp_path):
    settings = settings_for(tmp_path)
    directory = Path(settings.voicemail_storage_dir)
    directory.mkdir()
    private = tmp_path / "unrelated.json"
    private.write_text('{"private":"not-a-voicemail"}')
    (directory / f"{PARENT}.json").symlink_to(private)
    with TestClient(create_app(settings, gateway=Gateway())) as client:
        assert recording(client, settings).status_code == 400
        assert client.get("/api/voicemails").json()["voicemails"] == []
        assert private.read_text() == '{"private":"not-a-voicemail"}'
