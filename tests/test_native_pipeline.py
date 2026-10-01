"""Focused product and boundary checks; test helpers live in support."""

import asyncio
import io
import json
import os
import wave

import pytest

from bridge_pipeline import BridgePipeline
from call_details import CallDetailsStore
from call_quality import CallQualityRecorder, MAX_ACTIVE, MAX_WAV_BYTES
from media_capture import CaptureManager
from media_capture.capture import decode_mulaw
from transcription import TranscriptionManager

from support.native_pipeline import CALL, Detection, audio_sent, session
from support.transcription import Connector, result, settings, until


def test_native_recording_uses_remote_playback_once_and_keeps_speakers_separate(tmp_path):
    async def run():
        config, connector = settings(tmp_path), Connector()
        config.call_details_storage_dir = str(tmp_path / "details")
        quality = CallQualityRecorder(config)
        transcript = TranscriptionManager(config, connector)
        capture, detection = CaptureManager(config), Detection()
        pipeline = BridgePipeline(config, capture, transcript, detection, CallDetailsStore(""), quality=quality)
        call = session()
        caller, owner, mix, bot = (bytes([value]) * 160 for value in (1, 2, 3, 4))
        await pipeline.start(call)
        quality.audio(CALL, "generated", bot, 10, phase="1:reply-1")
        quality.event(CALL, "audio-speech-stats", 40, frames=1, underruns=0,
                      prompt="private prompt", token="private token")
        assert quality.snapshot_live(CALL)["completed"] is False
        pipeline.native_audio(call, "remote", "inbound", caller, 0)
        pipeline.native_audio(call, "remote", "outbound", mix, 0)
        pipeline.native_audio(call, "owner", "inbound", owner, 0)
        pipeline.native_audio(call, "owner", "outbound", caller, 20)
        pipeline.native_audio(call, "agent", "inbound", mix, 20)
        pipeline.output(call, bot, 20, "agent")
        pipeline.native_audio(call, "remote", "outbound", bot, 20)
        await until(lambda: len(connector.sockets) == 2)
        await until(lambda: len(audio_sent(connector.sockets[1])) == len(owner))
        assert audio_sent(connector.sockets[0]) == caller
        assert audio_sent(connector.sockets[1]) == owner
        assert detection.frames == [(CALL, "inbound", 0, caller)]

        connector.sockets[0].push(result("Caller question.", duration=.02))
        connector.sockets[1].push(result("Owner answer.", duration=.02))
        await until(lambda: len(transcript.call_segments(CALL)) == 2)
        await pipeline.agent_turn(call, "Agent answer.", 20, 40, agent_name="Admissions")
        await pipeline.agent_turn(call, "Interrupted reply.", 40, 60,
                                  agent_name="Admissions", delivery="interrupted")
        context = pipeline.context(call)
        assert [row["speaker"] for row in context] == ["remote", "owner", "agent", "agent"]
        assert [row["delivery"] for row in context[-2:]] == ["played", "interrupted"]

        await pipeline.end(call)
        await quality.close()
        await until(lambda: transcript.active_count == 0)
        with wave.open(str(tmp_path / "audio" / CALL / "inbound.wav")) as wav:
            assert wav.readframes(wav.getnframes()) == decode_mulaw(caller)
        with wave.open(str(tmp_path / "audio" / CALL / "outbound.wav")) as wav:
            assert wav.readframes(wav.getnframes()) == decode_mulaw(mix + bot)
        assert detection.ends == [(CALL, "call-ended")]
        diagnostics = quality.snapshot(CALL)
        assert diagnostics["completed"] and diagnostics["audio"]["sent"]["total_bytes"] == 160
        assert "private" not in json.dumps(diagnostics)
        for track in ("generated", "sent"):
            with wave.open(io.BytesIO(quality.read_audio(CALL, track))) as wav:
                assert wav.readframes(wav.getnframes()) == decode_mulaw(bot)
        assert quality.read_audio(CALL, "../../inbound") is None
        assert os.stat(quality.path).st_mode & 0o777 == 0o700
        assert os.stat(quality.path / CALL).st_mode & 0o777 == 0o700
        assert quality.pending_count == 0

        # Diagnostics refuse symlinks and files larger than the captured audio
        # bound, so an owner WAV route cannot serve another private file.
        original = quality.path / CALL / "sent.wav"
        original.unlink()
        outside = tmp_path / "outside.wav"
        outside.write_bytes(b"private-other-file")
        original.symlink_to(outside)
        assert quality.read_audio(CALL, "sent") is None
        original.unlink()
        with original.open("wb") as output:
            output.truncate(MAX_WAV_BYTES + 1)
        assert quality.read_audio(CALL, "sent") is None

        # Slow saves count against the same buffer limit as active recordings.
        # Skip extra diagnostic capture instead of queuing unbounded raw audio.
        quality.writes.update(object() for _ in range(MAX_ACTIVE))
        quality.start("CA" + "f" * 32)
        assert not quality.calls
        quality.writes.clear()
        await capture.close()
        await transcript.close()

    asyncio.run(run())


@pytest.mark.parametrize("muted,phase,suppressed", [
    (True, "connected", True),
    (False, "owner_ringing", True),
    (False, "connected", False),
])
def test_native_owner_transcription_excludes_unheard_microphone(tmp_path, muted, phase, suppressed):
    async def run():
        config, connector = settings(tmp_path), Connector()
        transcript = TranscriptionManager(config, connector)
        capture, detection = CaptureManager(config), Detection()
        pipeline = BridgePipeline(config, capture, transcript, detection, CallDetailsStore(""))
        call = session()
        call.native_owner_muted, call.phase = muted, phase
        caller, owner = b"\x01" * 160, b"\x02" * 160
        await pipeline.start(call)
        pipeline.native_audio(call, "remote", "inbound", caller, 0)
        pipeline.native_audio(call, "owner", "inbound", owner, 0)
        await until(lambda: len(connector.sockets) == 2)
        await until(lambda: len(audio_sent(connector.sockets[1])) == len(owner))
        assert audio_sent(connector.sockets[1]) == (b"\xff" * 160 if suppressed else owner)
        assert detection.frames == [(CALL, "inbound", 0, caller)]
        await pipeline.end(call)
        manifest = json.loads((tmp_path / "audio" / CALL / "manifest.json").read_text())
        assert manifest["tracks"]["outbound"]["samples"] == 0
        await capture.close()
        await transcript.close()

    asyncio.run(run())
