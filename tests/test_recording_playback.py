"""Browser playback, arbitrary byte seeking, and the on-disk trust boundary."""

import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
import os
import struct
from types import SimpleNamespace
import wave

import pytest
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from media_capture import playback
from media_capture.playback import RecordingLibrary

CALL = "CA" + "1" * 32
STREAM = "MZ" + "2" * 32


def write_capture(root, *, sid=CALL, inbound=(100, -200, 300, -400), outbound=(11, 22),
                  status="completed", minute=0):
    directory = root / sid
    directory.mkdir(parents=True)
    tracks = {}
    for name, samples in (("inbound", inbound), ("outbound", outbound)):
        with wave.open(str(directory / (name + ".wav")), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(struct.pack("<" + "h" * len(samples), *samples))
        tracks[name] = {"file": name + ".wav", "samples": len(samples),
                        "meaning": "caller-input" if name == "inbound" else "caller-playback"}
    started = datetime(2026, 9, 26, tzinfo=timezone.utc) + timedelta(minutes=minute)
    manifest = {"schema_version": 1, "call_sid": sid, "stream_sid": STREAM,
                "account_sid": "AC" + "3" * 32, "started_at": started.isoformat(),
                "finished_at": (started + timedelta(seconds=10)).isoformat(), "status": status,
                "sample_rate": 8000, "channels": 1, "sample_width": 2, "encoding": "pcm_s16le",
                "tracks": tracks, "counters": {"dropped_messages": 0, "rejected_messages": 0}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory, manifest


def save_manifest(directory, manifest):
    (directory / "manifest.json").write_text(json.dumps(manifest))


def library(root, *, enabled=True):
    return RecordingLibrary(SimpleNamespace(media_storage_dir=str(root), media_capture_enabled=enabled))


def request(method="GET", **headers):
    return Request({"type": "http", "method": method,
                    "headers": [(key.encode(), value.encode()) for key, value in headers.items()]})


def client_for(lib):
    async def catalog(req):
        return JSONResponse(await asyncio.to_thread(lib.snapshot))
    async def audio(req):
        return await asyncio.to_thread(lib.response, req.path_params["sid"],
                                       req.query_params.get("track", "combined"), req)
    return TestClient(Starlette(routes=[Route("/api/recordings", catalog),
        Route("/api/recordings/{sid}/audio", audio, methods=["GET", "HEAD"])]))


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


@pytest.mark.parametrize("track", ["combined", "inbound", "outbound"])
@pytest.mark.parametrize("start,end", [(0, 0), (0, 43), (3, 9), (42, 47), (44, 45), (45, 46), (46, 47)])
def test_byte_ranges_match_exact_file_bytes_including_unaligned_stereo_samples(tmp_path, track, start, end):
    root = tmp_path / "recordings"
    write_capture(root)
    with client_for(library(root)) as client:
        url = f"/api/recordings/{CALL}/audio?track={track}"
        full = client.get(url)
        ranged = client.get(url, headers={"Range": f"bytes={start}-{end}"})
        assert ranged.status_code == 206
        assert ranged.content == full.content[start:end + 1]
        assert ranged.headers["content-range"] == f"bytes {start}-{end}/{len(full.content)}"
        assert int(ranged.headers["content-length"]) == end - start + 1


@pytest.mark.parametrize("value,expected", [("bytes=-7", slice(-7, None)), ("bytes=45-", slice(45, None)),
    ("bytes=-999999", slice(None)), ("bytes=45-999999", slice(45, None))])
def test_suffix_and_open_ended_ranges(tmp_path, value, expected):
    root = tmp_path / "recordings"
    write_capture(root)
    with client_for(library(root)) as client:
        url = f"/api/recordings/{CALL}/audio"
        full = client.get(url).content
        response = client.get(url, headers={"Range": value})
        assert response.status_code == 206 and response.content == full[expected]


@pytest.mark.parametrize("value", ["bytes=60-", "bytes=8-2", "bytes=-0", "bytes=-", "bytes=",
    "bytes=1-2,4-5", "samples=0-10", "bytes=+2-4", "bytes=1-" + "9" * 100, "garbage"])
def test_invalid_ranges_are_416_without_audio_or_paths(tmp_path, value):
    root = tmp_path / "recordings"
    write_capture(root)
    with client_for(library(root)) as client:
        response = client.get(f"/api/recordings/{CALL}/audio", headers={"Range": value})
        assert response.status_code == 416 and response.content == b""
        assert response.headers["content-range"] == "bytes */60"
        assert response.headers["accept-ranges"] == "bytes"


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


def test_stale_if_range_returns_whole_file(tmp_path):
    root = tmp_path / "recordings"
    write_capture(root)
    with client_for(library(root)) as client:
        url = f"/api/recordings/{CALL}/audio"
        response = client.get(url, headers={"Range": "bytes=45-46", "If-Range": '"stale"'})
        assert response.status_code == 200 and len(response.content) == 60
        response = client.get(url, headers={"Range": "bytes=45-46", "If-Range": response.headers["etag"]})
        assert response.status_code == 206 and len(response.content) == 2


@pytest.mark.parametrize("spec", ["2.0", "2.4"])
def test_disconnect_closes_audio_even_before_generator_finishes(tmp_path, spec):
    root = tmp_path / "recordings"
    write_capture(root, inbound=(100,) * 20_000)
    response = library(root).response(CALL, "combined", request())
    fds = [track.fd for track in response.capture.tracks.values()]
    async def run():
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            if spec == "2.4" and message["type"] == "http.response.body":
                raise OSError("fixture disconnected")
        if spec == "2.4":
            with pytest.raises(ClientDisconnect):
                await response({"type": "http", "asgi": {"spec_version": spec}}, receive, send)
        else:
            await response({"type": "http", "asgi": {"spec_version": spec}}, receive, send)
    asyncio.run(run())
    for fd in fds:
        with pytest.raises(OSError): os.fstat(fd)


def test_combined_stream_uses_bounded_chunks_and_exact_arbitrary_slices(tmp_path):
    root = tmp_path / "recordings"
    write_capture(root, inbound=tuple(range(20_000)), outbound=tuple(range(-15_000, 0)))
    capture = library(root)._open(CALL)
    try:
        chunks = list(capture.chunks("combined", 0, capture.total("combined") - 1))
        assert len(chunks) > 1 and max(map(len, chunks)) <= playback.CHUNK_BYTES
        full = b"".join(chunks)
        for start in (1, 41, 43, 44, 45, 46, 47, 32_767, 32_769, 60_043):
            for count in (1, 2, 3, 7, 63):
                assert capture.read("combined", start, count) == full[start:start + count]
    finally:
        capture.close()


@pytest.mark.parametrize("change", ["wrong_call", "wrong_stream", "wrong_rate", "wrong_channels", "boolean_samples",
    "too_long", "sample_mismatch", "wrong_filename", "wrong_meaning", "missing_timestamp", "bad_timestamp",
    "reversed_times", "active", "failed", "header_rate", "truncated_wav", "extra_wav_bytes", "malformed_json"])
def test_invalid_or_unfinished_capture_is_not_cataloged_or_served(tmp_path, change):
    root = tmp_path / "recordings"
    directory, manifest = write_capture(root)
    if change == "wrong_call": manifest["call_sid"] = "CA" + "a" * 32
    elif change == "wrong_stream": manifest["stream_sid"] = "MZinvalid"
    elif change == "wrong_rate": manifest["sample_rate"] = 16_000
    elif change == "wrong_channels": manifest["channels"] = 2
    elif change == "boolean_samples": manifest["tracks"]["inbound"]["samples"] = True
    elif change == "too_long": manifest["tracks"]["inbound"]["samples"] = playback.MAX_SAMPLES + 1
    elif change == "sample_mismatch": manifest["tracks"]["inbound"]["samples"] = 3
    elif change == "wrong_filename": manifest["tracks"]["inbound"]["file"] = "../outside.wav"
    elif change == "wrong_meaning": manifest["tracks"]["outbound"]["meaning"] = "isolated-callee"
    elif change == "missing_timestamp": del manifest["finished_at"]
    elif change == "bad_timestamp": manifest["finished_at"] = "not a timestamp"
    elif change == "reversed_times": manifest["finished_at"] = "2025-01-01T00:00:00+00:00"
    elif change in {"active", "failed"}: manifest["status"] = change
    elif change == "header_rate":
        data = bytearray((directory / "inbound.wav").read_bytes())
        data[24:28] = struct.pack("<I", 16_000)
        (directory / "inbound.wav").write_bytes(data)
    elif change == "truncated_wav":
        (directory / "inbound.wav").write_bytes((directory / "inbound.wav").read_bytes()[:-1])
    elif change == "extra_wav_bytes":
        with (directory / "inbound.wav").open("ab") as output: output.write(b"extra")
    save_manifest(directory, manifest)
    if change == "malformed_json": (directory / "manifest.json").write_text("[not JSON")
    lib = library(root)
    assert lib.snapshot()["recordings"] == []
    with pytest.raises(HTTPException) as caught:
        lib.response(CALL, "combined", request())
    assert caught.value.status_code == 404 and str(tmp_path) not in caught.value.detail
    assert caught.value.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("part", ["root", "ancestor", "call", "manifest", "inbound", "outbound"])
def test_symlinks_are_never_followed(tmp_path, part):
    root = tmp_path / "actual" / "recordings"
    directory, _ = write_capture(root)
    if part == "root":
        link = tmp_path / "linked"
        link.symlink_to(root, target_is_directory=True)
        root = link
    elif part == "ancestor":
        link = tmp_path / "linked"
        link.symlink_to(root.parent, target_is_directory=True)
        root = link / "recordings"
    elif part == "call":
        original = directory.with_name("original")
        directory.rename(original)
        directory.symlink_to(original, target_is_directory=True)
    else:
        target = directory / ("manifest.json" if part == "manifest" else part + ".wav")
        original = target.with_name("original")
        target.rename(original)
        target.symlink_to(original)
    lib = library(root)
    assert lib.snapshot()["recordings"] == []
    with pytest.raises(HTTPException) as caught: lib.response(CALL, "combined", request())
    assert caught.value.status_code == 404


@pytest.mark.parametrize("part", ["manifest.json", "inbound.wav", "outbound.wav"])
def test_fifo_cannot_block_catalog_or_audio_request(tmp_path, part):
    root = tmp_path / "recordings"
    directory, _ = write_capture(root)
    target = directory / part
    target.unlink()
    os.mkfifo(target)
    lib = library(root)
    assert lib.snapshot()["recordings"] == []
    with pytest.raises(HTTPException) as caught: lib.response(CALL, "combined", request())
    assert caught.value.status_code == 404


@pytest.mark.parametrize("sid", ["../secret", "CA" + "z" * 32, "CA" + "a" * 33, CALL + "/../secret", "", "/etc/passwd"])
def test_identifier_traversal_rejected_before_file_access(tmp_path, sid):
    lib = library(tmp_path)
    with pytest.raises(HTTPException) as caught: lib.response(sid, "combined", request())
    assert caught.value.status_code == 404


def test_catalog_includes_partial_archives_when_capture_disabled_and_caps_ten(tmp_path):
    root = tmp_path / "recordings"
    for index in range(13):
        write_capture(root, sid=f"CA{index:032x}", minute=index,
                      status="partial" if index == 12 else "completed")
    lib = library(root, enabled=False)
    catalog = lib.snapshot()
    assert catalog["enabled"] and len(catalog["recordings"]) == 10
    assert catalog["recordings"][0]["call_sid"] == f"CA{12:032x}"
    assert catalog["recordings"][0]["status"] == "partial"
    assert catalog["recordings"][-1]["call_sid"] == f"CA{3:032x}"


def test_catalog_cache_and_scan_bound(tmp_path, monkeypatch):
    root = tmp_path / "recordings"
    root.mkdir()
    for index in range(20): (root / f"CA{index:032x}").mkdir()
    lib = library(root)
    calls = []
    def reject(sid, root_fd=None):
        calls.append(sid)
        raise ValueError("fixture invalid")
    monkeypatch.setattr(playback, "MAX_SCAN", 7)
    monkeypatch.setattr(lib, "_open", reject)
    assert lib.snapshot()["recordings"] == []
    assert len(calls) == 7
    assert lib.snapshot()["recordings"] == [] and len(calls) == 7
    lib._cached_at -= playback.CACHE_SECONDS + 1
    assert lib.snapshot()["recordings"] == [] and len(calls) == 14


def test_missing_manifest_empty_audio_disabled_storage_and_invalid_track(tmp_path):
    root = tmp_path / "recordings"
    directory, _ = write_capture(root)
    (directory / "manifest.json").unlink()
    assert library(root).snapshot()["recordings"] == []
    empty = tmp_path / "empty"
    write_capture(empty, inbound=(), outbound=())
    assert library(empty).snapshot()["recordings"] == []
    unconfigured = RecordingLibrary(SimpleNamespace(media_storage_dir="", media_capture_enabled=False))
    assert unconfigured.snapshot() == {"enabled": False, "recordings": [], "storage_error": ""}
    with pytest.raises(HTTPException) as caught: unconfigured.response(CALL, "combined", request())
    assert caught.value.status_code == 404
    with pytest.raises(HTTPException) as caught: library(root).response(CALL, "../secret", request())
    assert caught.value.status_code == 400
    assert library(tmp_path / "not-created-yet").snapshot() == {
        "enabled": True, "recordings": [], "storage_error": ""}


def test_response_rechecks_files_after_cached_catalog(tmp_path):
    root = tmp_path / "recordings"
    directory, _ = write_capture(root)
    lib = library(root)
    assert len(lib.snapshot()["recordings"]) == 1
    (directory / "inbound.wav").unlink()
    (directory / "inbound.wav").symlink_to(directory / "outbound.wav")
    with pytest.raises(HTTPException) as caught: lib.response(CALL, "combined", request())
    assert caught.value.status_code == 404
