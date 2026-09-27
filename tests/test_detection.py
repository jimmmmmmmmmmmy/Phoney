"""Transport-contract tests for the isolated offline Modulate stream adapter."""

import asyncio
import io
from pathlib import Path
import json
import subprocess
import sys
import wave

import httpx

from integrations.contracts import AudioFrame
from partner_detection.modulate import detect_audio, stream_inbound_pcm


class FakeSocket:
    """A small fake streaming server: results are released after text EOF."""

    def __init__(self, messages, *, never_finish=False):
        self.messages = list(messages)
        self.never_finish = never_finish
        self.sent = []
        self.closed = False
        self.ended = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def send(self, message):
        self.sent.append(message)
        if message == "":
            self.ended.set()

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        await self.ended.wait()
        if self.never_finish:
            await asyncio.Event().wait()
        for message in self.messages:
            yield json.dumps(message)


class FakeConnector:
    def __init__(self, socket):
        self.socket = socket
        self.url = None

    def __call__(self, url):
        self.url = url
        return self.socket


async def frames(*items):
    for item in items:
        yield item


def frame(track="inbound", payload=b"\x01\x00" * 160, timestamp=0):
    return AudioFrame("CA-local", "MZ-local", track, timestamp, payload)


def run(coro):
    return asyncio.run(coro)


def wav_clip(seconds=4):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x01\x00" * 8000 * seconds)
    return output.getvalue()


def test_batch_uploads_wav_with_documented_contract_and_normalizes_frames():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={
            "filename": "clip.wav",
            "duration_ms": 4000,
            "frames": [{"start_time_ms": 0, "end_time_ms": 4000,
                        "verdict": "synthetic", "confidence": 0.93}],
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = detect_audio(wav_clip(), filename="clip.wav", api_key="batch-key",
                              session_id="CA-local", track="inbound", client=client)

    request = requests[0]
    assert str(request.url) == "https://platform.modulate.ai/api/velma-2-synthetic-voice-detection-batch"
    assert request.headers["x-api-key"] == "batch-key"
    assert "multipart/form-data" in request.headers["content-type"]
    assert b'name="upload_file"; filename="clip.wav"' in request.content
    assert b"Content-Type: audio/wav" in request.content
    assert result["status"] == "ok"
    assert result["frames"][0]["verdict"] == "synthetic"
    assert result["frames"][0]["confidence"] == 0.93
    assert result["frames"][0]["synthetic_probability"] is None


def test_batch_accepts_mp3_and_preserves_source_timeline():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={
            "filename": "sample.mp3",
            "duration_ms": 5000,
            "frames": [{"start_time_ms": 1000, "end_time_ms": 3000,
                        "verdict": "non-synthetic", "confidence": 0.88}],
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = detect_audio(b"ID3" + b"\x00" * 128, filename="sample.mp3", api_key="batch-key",
                              session_id="local-file", track="inbound", source_start_ms=250,
                              client=client)

    assert b'name="upload_file"; filename="sample.mp3"' in requests[0].content
    assert b"Content-Type: audio/mpeg" in requests[0].content
    assert result["duration_ms"] == 5000
    assert result["frames"][0]["start_ms"] == 1250
    assert result["frames"][0]["end_ms"] == 3250


def test_batch_cli_is_a_no_network_dry_run_by_default(tmp_path):
    audio = tmp_path / "sample.mp3"
    audio.write_bytes(b"ID3" + b"\x00" * 128)
    script = Path(__file__).resolve().parents[1] / "scripts" / "detect_audio.py"
    result = subprocess.run([sys.executable, str(script), str(audio)], text=True,
                            capture_output=True, check=True)
    output = json.loads(result.stdout)
    assert output == {
        "event": "batch_detection_dry_run",
        "file": str(audio.resolve()),
        "network": False,
        "reason": "pass --send-to-provider to transmit audio",
        "stage": "offline_only",
    }


def test_stream_sends_only_inbound_pcm_then_text_eof_and_waits_for_done():
    socket = FakeSocket([
        {"type": "frame", "frame": {"start_time_ms": 0, "end_time_ms": 20,
                                      "verdict": "non-synthetic", "confidence": 0.91}},
        {"type": "done", "duration_ms": 20, "frame_count": 1},
    ])
    connector = FakeConnector(socket)
    report = run(stream_inbound_pcm(frames(frame(), frame("outbound", b"\x02\x00" * 160)),
                                    api_key="private-test-key", connector=connector))
    assert socket.sent == [b"\x01\x00" * 160, ""]
    assert socket.closed is True
    assert "api_key=private-test-key" in connector.url  # documented query authentication
    assert "audio_format=s16le" in connector.url
    assert report.status == "non-synthetic"
    assert report.observations[0].provider_verdict == "non-synthetic"
    assert report.observations[0].confidence == 0.91
    assert report.observations[0].confidence_kind == "verdict_confidence"
    assert report.submitted_audio_ms == 20


def test_no_content_is_unknown_but_retains_provider_verdict_and_confidence():
    socket = FakeSocket([
        {"type": "frame", "frame": {"start_time_ms": 0, "end_time_ms": 20,
                                      "verdict": "no-content", "confidence": 0.7}},
        {"type": "done", "duration_ms": 20, "frame_count": 1},
    ])
    report = run(stream_inbound_pcm(frames(frame()), api_key="key", connector=FakeConnector(socket)))
    assert report.status == "unknown"
    assert report.reason == "no_usable_content"
    assert report.observations[0].verdict == "unknown"
    assert report.observations[0].provider_verdict == "no-content"
    assert report.observations[0].confidence == 0.7


def test_provider_error_and_timeout_are_unknown_and_do_not_leak_key():
    error_socket = FakeSocket([{"type": "error", "message": "secret details"}])
    errored = run(stream_inbound_pcm(frames(frame()), api_key="do-not-leak", connector=FakeConnector(error_socket)))
    assert errored.status == "unknown"
    assert errored.reason == "provider_reported_error"
    assert "do-not-leak" not in json.dumps(errored.to_dict())

    timeout_socket = FakeSocket([], never_finish=True)
    timed_out = run(stream_inbound_pcm(frames(frame()), api_key="do-not-leak", deadline_seconds=1,
                                       connector=FakeConnector(timeout_socket)))
    assert timed_out.status == "unknown"
    assert timed_out.reason == "provider_timeout"
    assert timeout_socket.closed is True


def test_missing_done_and_invalid_frames_are_unknown():
    socket = FakeSocket([{"type": "frame", "frame": {"start_time_ms": 20, "end_time_ms": 10,
                                                       "verdict": "synthetic", "confidence": 1}}])
    report = run(stream_inbound_pcm(frames(frame()), api_key="key", connector=FakeConnector(socket)))
    assert report.status == "unknown"
    assert report.reason == "invalid_provider_response"


def make_capture(tmp_path: Path) -> Path:
    tracks = {}
    for name, value in (("inbound", b"\x01\x00"), ("outbound", b"\x02\x00")):
        path = tmp_path / f"{name}.wav"
        with wave.open(str(path), "wb") as output:
            output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            output.writeframes(value * 160)
        tracks[name] = {"file": path.name, "meaning": "caller-input" if name == "inbound" else "caller-playback",
                        "samples": 160, "gap_samples": 0}
    manifest = {"schema_version": 1, "status": "completed", "call_sid": "CA-local", "stream_sid": "MZ-local",
                "sample_rate": 8000, "channels": 1, "sample_width": 2, "encoding": "pcm_s16le",
                "tracks": tracks, "counters": {"dropped_messages": 0, "rejected_messages": 0}}
    result = tmp_path / "manifest.json"
    result.write_text(json.dumps(manifest))
    return result


def test_cli_is_dry_run_by_default_and_never_needs_a_key(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "detect_capture.py"
    result = subprocess.run([sys.executable, str(script), str(make_capture(tmp_path))], text=True,
                            capture_output=True, check=True)
    output = json.loads(result.stdout)
    assert output["event"] == "detection_dry_run"
    assert output["network"] is False
    assert output["track"] == "inbound"


def test_stream_cli_accepts_an_explicit_environment_file(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "detect_capture.py"
    env_file = tmp_path / "detection.env"
    env_file.write_text("MODULATE_API_KEY=\n")
    result = subprocess.run([
        sys.executable, str(script), str(make_capture(tmp_path)),
        "--send-to-provider", "--env-file", str(env_file),
    ], text=True, capture_output=True)
    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "missing_api_key"


def response_frames(duration=20, verdict="non-synthetic", confidence=0.94):
    return [
        {"type": "frame", "frame": {"start_time_ms": 0, "end_time_ms": duration,
                                      "verdict": verdict, "confidence": confidence}},
        {"type": "done", "duration_ms": duration, "frame_count": 1},
    ]


def test_stream_translates_initial_nonzero_timestamp_without_padding():
    socket = FakeSocket(response_frames(40))
    report = run(stream_inbound_pcm(frames(frame(timestamp=180), frame(timestamp=200)),
                                    api_key="key", connector=FakeConnector(socket)))
    assert report.status == "non-synthetic"
    assert report.source_start_ms == 180
    assert report.observations[0].start_ms == 180
    assert report.observations[0].end_ms == 220
    assert socket.sent == [b"\x01\x00" * 160, b"\x01\x00" * 160, ""]


def test_missing_or_overlapping_audio_cannot_produce_confident_evidence():
    for timestamp in (10, 40):
        socket = FakeSocket(response_frames(40))
        report = run(stream_inbound_pcm(frames(frame(timestamp=0), frame(timestamp=timestamp)),
                                        api_key="key", connector=FakeConnector(socket)))
        assert report.status == "unknown"
        assert report.reason == "audio_discontinuity"
        assert report.observations == ()
        assert socket.closed


def test_collection_can_outlast_finalization_deadline():
    async def slow_frames():
        yield frame()
        await asyncio.sleep(0.08)
        yield frame(timestamp=20)

    socket = FakeSocket(response_frames(40))
    report = run(stream_inbound_pcm(slow_frames(), api_key="key", deadline_seconds=0.03,
                                    collection_seconds=0.5, connector=FakeConnector(socket)))
    assert report.status == "non-synthetic"
    assert report.submitted_audio_ms == 40


def test_audio_limit_sends_eof_and_preserves_partial_coverage():
    socket = FakeSocket(response_frames(1000))
    report = run(stream_inbound_pcm(frames(frame(payload=b"\x01\x00" * 12000)),
                                    api_key="key", max_audio_seconds=1,
                                    connector=FakeConnector(socket)))
    assert report.status == "non-synthetic"
    assert report.coverage_limited is True
    assert report.submitted_audio_ms == 1000
    assert socket.sent == [b"\x01\x00" * 8000, ""]


def test_collection_stall_and_eof_stall_have_separate_sanitized_failures():
    async def stalled_frames():
        yield frame()
        await asyncio.Event().wait()

    socket = FakeSocket(response_frames())
    report = run(stream_inbound_pcm(stalled_frames(), api_key="key", collection_seconds=0.02,
                                    connector=FakeConnector(socket)))
    assert report.reason == "audio_collection_timeout"
    assert socket.closed

    class StalledEOF(FakeSocket):
        async def send(self, message):
            if message == "":
                await asyncio.Event().wait()
            await super().send(message)

    socket = StalledEOF(response_frames())
    report = run(stream_inbound_pcm(frames(frame()), api_key="key", deadline_seconds=0.02,
                                    connector=FakeConnector(socket)))
    assert report.reason == "provider_timeout"
    assert socket.closed


def test_provider_error_cancels_input_without_waiting_for_call_end():
    class EarlyError(FakeSocket):
        async def _messages(self):
            yield json.dumps({"type": "error", "message": "key=secret"})

    async def endless_frames():
        yield frame()
        await asyncio.Event().wait()

    async def scenario():
        socket = EarlyError([])
        report = await asyncio.wait_for(stream_inbound_pcm(endless_frames(), api_key="secret",
                                    connector=FakeConnector(socket)), timeout=0.3)
        return report, socket

    import time
    started = time.monotonic()
    report, socket = run(scenario())
    assert time.monotonic() - started < 1
    assert report.reason == "provider_reported_error"
    assert "secret" not in json.dumps(report.to_dict())
    assert socket.closed


def test_all_silent_pcm_remains_no_usable_content_despite_provider_verdict():
    socket = FakeSocket(response_frames())
    report = run(stream_inbound_pcm(frames(frame(payload=b"\0" * 320)), api_key="key",
                                    connector=FakeConnector(socket)))
    assert report.status == "unknown"
    assert report.reason == "no_usable_content"


def test_batch_malformed_verdict_is_sanitized():
    from partner_detection.modulate import ModulateError
    import pytest

    def handle(request):
        return httpx.Response(200, json={"duration_ms": 4000, "frames": [
            {"start_time_ms": 0, "end_time_ms": 4000, "verdict": [], "confidence": .9}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ModulateError, match="invalid_provider_frame"):
            detect_audio(wav_clip(), filename="clip.wav", api_key="key",
                         session_id="CA-test", track="inbound", client=client)


def test_capture_cli_rejects_missing_audio_before_provider_access(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "detect_capture.py"
    manifest_path = make_capture(tmp_path)
    for mutation in ("gap_samples", "dropped_messages", "rejected_messages"):
        manifest = json.loads(manifest_path.read_text())
        manifest["tracks"]["inbound"]["gap_samples"] = int(mutation == "gap_samples")
        manifest["counters"]["dropped_messages"] = int(mutation == "dropped_messages")
        manifest["counters"]["rejected_messages"] = int(mutation == "rejected_messages")
        manifest_path.write_text(json.dumps(manifest))
        result = subprocess.run([sys.executable, str(script), str(manifest_path), "--send-to-provider"],
                                text=True, capture_output=True)
        assert result.returncode == 2
        assert json.loads(result.stdout)["network"] is False
        assert json.loads(result.stdout)["reason"] == "incomplete_audio"


def test_batch_cli_report_file_is_owner_only(tmp_path):
    import stat
    audio = tmp_path / "sample.mp3"
    audio.write_bytes(b"ID3")
    output = tmp_path / "report.json"
    output.write_text("old")
    output.chmod(0o644)
    script = Path(__file__).resolve().parents[1] / "scripts" / "detect_audio.py"
    subprocess.run([sys.executable, str(script), str(audio), "--output", str(output)],
                   text=True, capture_output=True, check=True)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_stalled_binary_upload_has_its_own_timeout():
    class StalledUpload(FakeSocket):
        async def send(self, message):
            await asyncio.Event().wait()
    socket = StalledUpload([])
    report = run(stream_inbound_pcm(frames(frame()), api_key="key", deadline_seconds=.02,
                                    collection_seconds=.5, connector=FakeConnector(socket)))
    assert report.reason == "provider_timeout"
    assert socket.closed
