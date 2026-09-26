"""Signed capture becomes a playable public WAV only after finalization."""

import io
import wave

from test_media_webhooks import (media_app, PARENT, OTHER, stream_and_token, socket_headers,
                                 send_start, send_audio, stop_message, assert_socket_closed)
from test_transcription import until


def test_real_capture_pipeline_publishes_playable_audio_without_transcription(media_app):
    client, settings, gateway = media_app
    _, token = stream_and_token(client, settings)
    path = f"/api/recordings/{PARENT}/audio"
    with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)) as ws:
        send_start(ws, settings, token)
        send_audio(ws, track="inbound", sample=0)
        send_audio(ws, track="outbound", sample=128)
        assert client.get(path).status_code == 404  # Still writing; no finalized manifest.
        ws.send_json(stop_message(settings))
        assert assert_socket_closed(ws) == 1000
    client.portal.call(until, lambda: client.app.state.media_capture.active_count == 0)
    catalog = client.get("/api/recordings")
    assert catalog.status_code == 200
    recording = catalog.json()["recordings"][0]
    assert recording["call_sid"] == PARENT and recording["status"] == "completed"
    assert recording["duration_seconds"] == .02
    snapshot = client.get("/api/transcripts").json()
    assert snapshot["sessions"] == [] and snapshot["recordings"] == catalog.json()
    combined = client.get(recording["url"])
    assert combined.status_code == 200
    assert combined.headers["content-type"].startswith("audio/wav")
    assert combined.headers["cache-control"] == "no-store"
    assert "set-cookie" not in combined.headers
    with wave.open(io.BytesIO(combined.content), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (2, 2, 8000, 160)
        samples = wav.readframes(160)
        assert samples[:2] != samples[2:4]  # The two real decoded media tracks survive separately.
    head = client.head(recording["url"])
    assert head.status_code == 200 and head.content == b""
    assert int(head.headers["content-length"]) == len(combined.content)
    seek = client.get(recording["url"], headers={"Range": "bytes=47-101"})
    assert seek.status_code == 206 and seek.content == combined.content[47:102]
    assert seek.headers["content-range"] == f"bytes 47-101/{len(combined.content)}"
    for track in ("inbound", "outbound"):
        original = client.get(recording["tracks"][track]["url"])
        with wave.open(io.BytesIO(original.content), "rb") as wav:
            assert wav.getnchannels() == 1 and wav.getnframes() == 160
    assert settings.media_storage_dir not in catalog.text
    assert settings.account_sid not in catalog.text
    assert settings.auth_token not in catalog.text
    assert client.get(f"/api/recordings/{OTHER}/audio").status_code == 404
    assert gateway.ended_calls == []  # Listening cannot affect the telephone conference.


def test_audio_endpoints_are_read_only_and_do_not_expose_unknown_files(media_app):
    client, settings, _ = media_app
    assert client.get("/api/recordings").json()["recordings"] == []
    for method in ("post", "put", "patch", "delete"):
        for route in ("/api/recordings", f"/api/recordings/{PARENT}/audio"):
            assert getattr(client, method)(route).status_code == 405
    assert client.get("/internal/deploy").status_code == 403
    assert client.post("/voice", data={"AccountSid": settings.account_sid, "CallSid": PARENT}).status_code == 403
