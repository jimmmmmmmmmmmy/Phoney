"""The offline partner seam preserves track identity and never accepts partial audio."""

import json
from pathlib import Path
import subprocess
import sys
import wave

import pytest

from integrations.contracts import AudioFrame
from integrations.replay import MAX_MANIFEST_BYTES, read_completed_capture, replay_capture


class Observer:
    def __init__(self):
        self.frames = []
        self.finished = []

    def on_frame(self, frame):
        self.frames.append(frame)

    def on_end(self, capture):
        self.finished.append(capture)


@pytest.fixture
def capture(tmp_path):
    tracks = {}
    for track, samples, meaning, value in (
        ("inbound", 240, "caller-input", b"\x01\x00"),
        ("outbound", 320, "caller-playback", b"\x02\x00"),
    ):
        filename = f"{track}.wav"
        with wave.open(str(tmp_path / filename), "wb") as audio:
            audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(value * samples)
        tracks[track] = {"file": filename, "meaning": meaning, "samples": samples}
    manifest = {"schema_version": 1, "status": "completed", "call_sid": "CAtest", "stream_sid": "MZtest",
                "sample_rate": 8000, "channels": 1, "sample_width": 2, "encoding": "pcm_s16le", "tracks": tracks}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def rewrite(path, change):
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest))


def test_replay_preserves_pcm_tracks_and_capture_timeline(capture):
    observer = Observer()
    result = replay_capture(capture, observer)
    assert [(frame.track, frame.timestamp_ms, frame.sample_count) for frame in observer.frames] == [
        ("inbound", 0, 160), ("outbound", 0, 160), ("inbound", 20, 80), ("outbound", 20, 160)]
    assert observer.frames[0].pcm_s16le == b"\x01\x00" * 160
    assert observer.frames[1].pcm_s16le == b"\x02\x00" * 160
    assert result.frame_count == 4
    assert result.manifest_path == capture.resolve()
    assert observer.finished == [result]
    assert read_completed_capture(capture).duration_ms == 40


@pytest.mark.parametrize("status", ["active", "partial", "failed", None])
def test_no_consumer_receives_partial_capture(capture, status):
    rewrite(capture, lambda data: data.update(status=status))
    observer = Observer()
    with pytest.raises(ValueError, match="completed"):
        replay_capture(capture, observer)
    assert observer.frames == observer.finished == []


@pytest.mark.parametrize("field,value", [("sample_rate", 16000), ("channels", 2),
                                        ("channels", True), ("encoding", "mulaw"), ("schema_version", True)])
def test_manifest_format_is_strict(capture, field, value):
    rewrite(capture, lambda data: data.update({field: value}))
    with pytest.raises(ValueError):
        read_completed_capture(capture)


def test_rejects_wav_path_traversal(capture):
    rewrite(capture, lambda data: data["tracks"]["inbound"].update(file="../outside.wav"))
    with pytest.raises(ValueError, match="adjacent"):
        read_completed_capture(capture)


def test_rejects_symlink_wav(capture):
    inbound = capture.parent / "inbound.wav"
    moved = capture.parent / "moved.wav"
    inbound.rename(moved)
    inbound.symlink_to(moved)
    with pytest.raises(ValueError, match="regular"):
        read_completed_capture(capture)


def test_checks_both_wav_headers_before_delivering_audio(capture):
    with wave.open(str(capture.parent / "outbound.wav"), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 320)
    observer = Observer()
    with pytest.raises(ValueError, match="WAV format"):
        replay_capture(capture, observer)
    assert observer.frames == []


def test_manifest_sample_count_must_match_wav(capture):
    rewrite(capture, lambda data: data["tracks"]["inbound"].update(samples=160))
    with pytest.raises(ValueError, match="sample count"):
        read_completed_capture(capture)


def test_truncated_wav_cannot_report_success(capture):
    outbound = capture.parent / "outbound.wav"
    with outbound.open("r+b") as audio:
        audio.truncate(outbound.stat().st_size - 2)
    observer = Observer()
    with pytest.raises(ValueError, match="declared length"):
        replay_capture(capture, observer)
    assert observer.finished == []


def test_oversized_manifest_is_rejected(capture):
    capture.write_text(" " * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(ValueError, match="64 KiB"):
        read_completed_capture(capture)


def test_consumer_failure_stops_replay_without_end_callback(capture):
    class BrokenObserver(Observer):
        def on_frame(self, frame):
            raise RuntimeError("partner callback failed")

    observer = BrokenObserver()
    with pytest.raises(RuntimeError, match="partner callback"):
        replay_capture(capture, observer)
    assert observer.finished == []


def test_audio_frame_never_exposes_payload_in_repr():
    frame = AudioFrame("CA", "MZ", "inbound", 0, b"private audio!")
    assert "private" not in repr(frame)


@pytest.mark.parametrize("overrides", [{"track": "callee"}, {"timestamp_ms": -1},
                                      {"sample_rate": 16000}, {"pcm_s16le": b"x"}])
def test_audio_frame_rejects_invalid_format(overrides):
    arguments = dict(session_id="CA", stream_id="MZ", track="inbound", timestamp_ms=0, pcm_s16le=b"\x00\x00")
    with pytest.raises(ValueError):
        AudioFrame(**(arguments | overrides))


def test_cli_defaults_to_one_metadata_summary(capture):
    script = Path(__file__).resolve().parents[1] / "scripts" / "replay_capture.py"
    result = subprocess.run([sys.executable, str(script), str(capture)], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["event"] == "capture_summary"
    assert summary["replayed_frames"] == 4
    assert "payload" not in result.stdout
    assert "pcm_s16le" not in result.stdout
