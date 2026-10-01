"""Focused product and boundary checks; test helpers live in support."""

import io
import json
import os
import struct
import wave

import pytest

from media_capture import playback

from support.recording_playback import CALL, client_for, library, request, write_capture


def test_catalog_and_stereo_playback_need_no_transcript_or_external_encoder(tmp_path):
    root = tmp_path / "recordings"
    directory, _ = write_capture(root)
    with client_for(library(root)) as client:
        catalog = client.get("/api/recordings").json()
        assert catalog["enabled"] and catalog["storage_error"] == ""
        recording, = catalog["recordings"]
        assert recording["call_sid"] == CALL and recording["status"] == "completed"
        assert recording["duration_seconds"] == 4 / 8000
        assert set(recording["tracks"]) == {"inbound", "outbound"}
        assert str(tmp_path) not in json.dumps(catalog) and "account_sid" not in json.dumps(catalog)
        response = client.get(recording["url"])
        assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-disposition"] == f'inline; filename="{CALL}-combined.wav"'
        assert response.headers["referrer-policy"] == "no-referrer"
        assert int(response.headers["content-length"]) == len(response.content) == 60
        with wave.open(io.BytesIO(response.content), "rb") as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (2, 2, 8000)
            assert struct.unpack("<8h", audio.readframes(4)) == (100, 11, -200, 22, 300, 0, -400, 0)
        for track in ("inbound", "outbound"):
            audio = client.get(recording["tracks"][track]["url"])
            assert audio.content == (directory / (track + ".wav")).read_bytes()


def test_head_has_identical_metadata_no_body_and_closes_files(tmp_path, monkeypatch):
    root = tmp_path / "recordings"
    write_capture(root)
    opened = []
    original = playback._open_regular
    def tracking(*args):
        result = original(*args)
        opened.append(result[0])
        return result
    monkeypatch.setattr(playback, "_open_regular", tracking)
    lib = library(root)
    response = lib.response(CALL, "combined", request("HEAD", range="bytes=45-51"))
    assert response.status_code == 200 and response.body == b""
    assert response.headers["content-length"] == "60"
    assert "content-range" not in response.headers
    for fd in opened:
        with pytest.raises(OSError): os.fstat(fd)
