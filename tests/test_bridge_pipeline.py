"""Real local recording/transcript integration with fake STT and no phone calls."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import wave
import pytest

from bridge_pipeline import BridgePipeline
from call_details import CallDetailsStore, transcript_fingerprint
from gemini_summary import _request_body
from media_capture import CaptureManager
from media_capture.capture import decode_mulaw
from transcription import TranscriptionManager
from test_transcription import Connector, result, settings, until

CALL = "CA" + "a" * 32
STREAM = "MZ" + "b" * 32


class Detection:
    def __init__(self):
        self.frames = []
        self.starts = []
        self.ends = []

    def start(self, *args):
        self.starts.append(args)

    def offer(self, *args):
        self.frames.append(args)

    def finish(self, *args):
        self.ends.append(args)


def session():
    return SimpleNamespace(id="session-test", canonical_call_sid=CALL, to="+12025550101",
        created_at=datetime.now(timezone.utc).isoformat(), legs={
            "remote": SimpleNamespace(stream_sid=STREAM, generation=1),
            "owner": SimpleNamespace(stream_sid="MZ" + "c" * 32, generation=1)})


class Controller:
    def __init__(self):
        self.turns = []
        self.releases = []

    async def transcript(self, *args, **kwargs):
        self.turns.append((args, kwargs))

    async def set_mode(self, *args):
        self.releases.append(args)


def test_caller_control_uses_word_onset_after_leading_silence(tmp_path):
    async def run():
        config, connector = settings(tmp_path), Connector()
        transcript = TranscriptionManager(config, connector)
        capture = CaptureManager(config)
        pipeline = BridgePipeline(config, capture, transcript, Detection(), CallDetailsStore(""))
        pipeline.controller = Controller()
        transcript.on_segment = pipeline.transcript_event
        call = session()
        await pipeline.start(call)
        pipeline.audio(call, "remote", b"\xff" * 160, 3000)
        await until(lambda: len(connector.sockets) == 2)
        # The result window begins before agent playback at 2000 ms, but this
        # new spoken interruption begins afterward, at 2400 ms.
        message = result("Wait", start=1, duration=2)
        message["channel"]["alternatives"][0]["words"] = [
            {"word": "Wait", "start": 2.4, "end": 2.7}]
        connector.sockets[0].push(message)
        await until(lambda: len(pipeline.controller.turns) == 1)
        args, kwargs = pipeline.controller.turns[0]
        assert args == (call.id, "remote", "Wait")
        assert kwargs["timestamp_ms"] == 2400
        assert pipeline.context(call)[0]["start_ms"] == 1000
        # Providers without word timing retain the existing result timestamp.
        pipeline.transcript_event(CALL, {"track": "inbound", "text": "Another turn",
            "start_ms": 2800, "end_ms": 3000}, False)
        await until(lambda: len(pipeline.controller.turns) == 2)
        assert pipeline.controller.turns[1][1]["timestamp_ms"] == 2800
        await pipeline.close()
        await capture.close()
        await transcript.close()
    asyncio.run(run())


def test_transcription_failure_releases_agent_and_rejects_stale_context(tmp_path):
    async def run():
        config = settings(tmp_path)
        transcript = TranscriptionManager(config, Connector())
        capture = CaptureManager(config)
        pipeline = BridgePipeline(config, capture, transcript, Detection(), CallDetailsStore(""))
        pipeline.controller = Controller()
        transcript.on_failure = pipeline.transcription_failed
        call = session()
        await pipeline.start(call)
        pipeline.audio(call, "remote", b"\x01" * 160, 0)
        assert pipeline.context(call) == []
        live = transcript.sessions[CALL]
        transcript._fail(live, live.tracks["inbound"], "provider-unavailable")
        await until(lambda: pipeline.controller.releases)
        assert pipeline.controller.releases == [(call.id, "human")]
        with pytest.raises(RuntimeError, match="Transcription is unavailable"):
            pipeline.context(call)
        await pipeline.close()
        await capture.close()
        await transcript.close()
        assert not pipeline.active_call_ids and not pipeline.release_tasks
    asyncio.run(run())


def test_silent_voicemail_initializes_capture_and_transcript_before_first_audio(tmp_path):
    async def run():
        config = settings(tmp_path)
        transcript = TranscriptionManager(config, Connector())
        capture = CaptureManager(config)
        detection = Detection()
        pipeline = BridgePipeline(config, capture, transcript, detection, CallDetailsStore(""))
        call = session()
        call.voicemail = True
        await pipeline.start(call)
        assert CALL not in transcript.sessions
        assert pipeline.context(call) == []
        assert CALL in transcript.sessions and detection.starts == [(CALL, STREAM)]
        assert CALL in pipeline.started
        await pipeline.close()
        await capture.close()
        await transcript.close()
    asyncio.run(run())


def test_bridge_preserves_wavs_excludes_our_voice_and_persists_agent_attribution(tmp_path):
    async def run():
        config = settings(tmp_path / "transcripts")
        connector = Connector()
        transcript = TranscriptionManager(config, connector)
        capture = CaptureManager(config)
        detection = Detection()
        details = CallDetailsStore(str(tmp_path / "details"))
        pipeline = BridgePipeline(config, capture, transcript, detection, details)
        transcript.on_segment = pipeline.transcript_event
        pipeline.controller = Controller()
        call = session()
        await pipeline.start(call)
        remote, human, generated = bytes([1]) * 160, bytes([2]) * 160, bytes([3]) * 160
        pipeline.audio(call, "owner", generated, 0)  # Never caller evidence.
        pipeline.audio(call, "remote", remote, 0)
        pipeline.output(call, human, 0, "human")
        pipeline.output(call, generated, 20, "agent")
        await until(lambda: len(connector.sockets) == 2)
        await until(lambda: any(isinstance(x, bytes) for x in connector.sockets[1].sent))
        connector.sockets[0].push(result("A real caller question.", duration=.02))
        connector.sockets[1].push(result("The owner's answer.", duration=.02))
        await until(lambda: len(pipeline.controller.turns) == 2)
        await pipeline.agent_turn(call, "The agent's answer.", 20, 40, agent_name="Admissions")
        assert [row["speaker"] for row in pipeline.context(call)] == ["remote", "owner", "agent"]
        assert len(detection.frames) == 1
        assert detection.frames[0] == (CALL, "inbound", 0, remote)
        # The generated bytes are saved for playback but STT receives silence.
        outbound_stt = b"".join(x for x in connector.sockets[1].sent if isinstance(x, bytes))
        assert generated not in outbound_stt
        assert human in outbound_stt
        assert detection.starts == [(CALL, STREAM)]
        await pipeline.end(call)
        await until(lambda: transcript.active_count == 0)
        with wave.open(str(tmp_path / "transcripts" / "audio" / CALL / "inbound.wav")) as wav:
            assert wav.readframes(wav.getnframes()) == decode_mulaw(remote)
        with wave.open(str(tmp_path / "transcripts" / "audio" / CALL / "outbound.wav")) as wav:
            assert wav.readframes(wav.getnframes()) == decode_mulaw(human + generated)
        document = transcript.history[0]
        assert transcript_fingerprint(document)
        agent = document["segments"][-1]
        assert agent["source"] == "agent" and agent["speaker"] == "Admissions"
        assert agent["delivery"] == "played"
        assert pipeline.active_call_ids == set()
        reloaded = TranscriptionManager(config, Connector())
        assert reloaded.history[0]["segments"][-1] == agent
        prompt = json.loads(_request_body(document))
        summary_input = json.loads(prompt["contents"][0]["parts"][0]["text"])
        assert summary_input["segments"][-1]["source"] == "agent"
        await pipeline.close()
        await capture.close()
        await transcript.close()
        await reloaded.close()
    asyncio.run(run())


def test_reconnect_keeps_canonical_stream_and_records_provenance(tmp_path):
    async def run():
        config = settings(tmp_path)
        transcript = TranscriptionManager(config, Connector())
        capture, detection = CaptureManager(config), Detection()
        pipeline = BridgePipeline(config, capture, transcript, detection, CallDetailsStore(""))
        call = session()
        await pipeline.start(call)
        pipeline.audio(call, "remote", b"\x01" * 160, 0)
        call.legs["remote"].stream_sid = "MZ" + "d" * 32
        call.legs["remote"].generation = 2
        pipeline.audio(call, "remote", b"\x02" * 160, 200)
        assert detection.starts == [(CALL, STREAM)]
        assert transcript.sessions[CALL].stream_sid == STREAM
        await pipeline.end(call)
        manifest = json.loads((tmp_path / "audio" / CALL / "manifest.json").read_text())
        assert manifest["stream_sid"] == STREAM
        assert len(manifest["sources"]) == 3
        assert manifest["tracks"]["inbound"]["gap_samples"] == 1440
        await transcript.close()
    asyncio.run(run())


def test_callback_failures_do_not_kill_transcription_and_duplicate_finals_are_not_replayed(tmp_path):
    async def run():
        connector = Connector()
        observed = []

        def broken(sid, segment, final):
            observed.append((sid, segment, final))
            raise RuntimeError("Listener failed")

        manager = TranscriptionManager(settings(tmp_path), connector, on_segment=broken)
        manager.start(CALL, STREAM)
        manager.offer(CALL, "inbound", 0, b"\xff" * 160)
        await until(lambda: len(connector.sockets) == 2)
        connector.sockets[0].push(result("Hello."))
        connector.sockets[0].push(result("Hello."))
        await until(lambda: len(observed) == 1)
        await asyncio.sleep(.01)
        assert len(observed) == 1
        assert len(manager.sessions[CALL].segments) == 1
        manager.finish(CALL)
        await manager.close()
    asyncio.run(run())


def test_agent_provenance_changes_summary_identity_without_exporting_provider_history():
    doc = {"call_sid": CALL, "stream_sid": STREAM, "status": "completed",
           "ended_at": "2026-09-26T12:00:00+00:00", "segments": [{
               "track": "outbound", "start_ms": 0, "end_ms": 200,
               "text": "Hello.", "source": "agent", "speaker": "Agent One", "delivery": "played"}]}
    original = transcript_fingerprint(doc)
    other = deepcopy(doc)
    other["segments"][0]["speaker"] = "Agent Two"
    assert original != transcript_fingerprint(other)
    other["segments"][0]["delivery"] = "interrupted"
    assert original != transcript_fingerprint(other)
    other["segments"][0]["thoughtSignature"] = "private-provider-state"
    assert b"private-provider-state" not in _request_body(other)
    other["segments"][0]["track"] = "inbound"
    assert transcript_fingerprint(other) is None


def test_ai_voicemail_receipt_finishes_from_real_local_recording(tmp_path):
    from voicemail import VoicemailStore
    from media_capture.playback import RecordingLibrary
    async def run():
        config = settings(tmp_path / 'transcripts')
        receipts = VoicemailStore(SimpleNamespace(voicemail_enabled=False, voicemail_agent_enabled=True,
            voicemail_storage_dir=str(tmp_path/'voicemails'), voicemail_max_seconds=120))
        transcript = TranscriptionManager(config, Connector())
        capture = CaptureManager(config)
        recordings = RecordingLibrary(config)
        pipeline = BridgePipeline(config,capture,transcript,Detection(),CallDetailsStore(''),receipts,recordings)
        call = session()
        call.voicemail = True
        receipts.start(CALL,'owner-no-answer',mode='voicemail_ai',started_at=call.created_at)
        await pipeline.start(call)
        pipeline.audio(call,'remote',b'\x01'*160,0)
        pipeline.output(call,b'\x02'*160,0,'agent')
        await pipeline.end(call)
        await receipts.close()
        receipt = VoicemailStore(SimpleNamespace(voicemail_enabled=True,
            voicemail_storage_dir=receipts.path,voicemail_max_seconds=120)).get(CALL)
        assert receipt['mode']=='voicemail_ai' and receipt['recording_status']=='completed'
        assert receipt['recording_sid']=='' and receipt['ended_at']
        assert recordings.get(CALL)['url'].endswith('/audio?track=combined')
        await pipeline.close()
        await capture.close()
        await transcript.close()
    asyncio.run(run())


def test_failed_voicemail_transcription_uses_recording_fallback_once(tmp_path):
    class FallbackController(Controller):
        async def fallback_voicemail(self,*args):
            self.releases.append(args)
    async def run():
        config=settings(tmp_path)
        transcript=TranscriptionManager(config,Connector())
        capture=CaptureManager(config)
        pipeline=BridgePipeline(config,capture,transcript,Detection(),CallDetailsStore(''))
        pipeline.controller=FallbackController()
        transcript.on_failure=pipeline.transcription_failed
        call=session()
        call.voicemail=True
        await pipeline.start(call)
        pipeline.audio(call,'remote',b'\x01'*160,0)
        live=transcript.sessions[CALL]
        transcript._fail(live,live.tracks['inbound'],'provider-unavailable')
        transcript._fail(live,live.tracks['outbound'],'provider-unavailable')
        await until(lambda:pipeline.controller.releases)
        assert pipeline.controller.releases==[(call.id,'transcription-unavailable')]
        await pipeline.close()
        await capture.close()
        await transcript.close()
    asyncio.run(run())
