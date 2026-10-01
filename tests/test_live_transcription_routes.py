"""Focused product and boundary checks; test helpers live in support."""

import json
import asyncio
from types import SimpleNamespace

from transcription.service import TranscriptionManager, _Session

from support.live_transcription_routes import final_session, live_client, wait_for
from support.media_webhooks import PARENT, assert_socket_closed, read_manifest, send_audio, send_start, socket_headers, stop_message, stream_and_token
from support.transcription import result


def test_signed_two_track_audio_reaches_public_live_view_and_final_exports(tmp_path):
    with live_client(tmp_path) as (client, settings, gateway, connector):
        _, token = stream_and_token(client, settings)
        assert client.get("/api/transcripts").status_code == 200
        with client.websocket_connect(f"/media/{PARENT}/", headers=socket_headers(settings)) as socket:
            send_start(socket, settings, token)
            send_audio(socket, track="inbound", sample=0x00)
            send_audio(socket, track="outbound", sample=0x80)
            wait_for(client, lambda: len(connector.sockets) == 2 and all(
                any(isinstance(value, bytes) for value in provider.sent)
                for provider in connector.sockets))
            assert b"".join(value for value in connector.sockets[0].sent
                            if isinstance(value, bytes)) == b"\x00" * 160
            assert b"".join(value for value in connector.sockets[1].sent
                            if isinstance(value, bytes)) == b"\x80" * 160
            client.portal.call(connector.sockets[0].push, result("Still speaking", final=False))
            wait_for(client, lambda: final_session(client)["tracks"]["inbound"]["interim"] == "Still speaking")
            live = client.get("/api/transcripts").json()
            assert live["selected_call_sid"] == PARENT
            assert live["sessions"][0]["tracks"]["inbound"]["interim"] == "Still speaking"
            client.portal.call(connector.sockets[0].push, result("Caller words."))
            client.portal.call(connector.sockets[1].push, result("Playback words."))
            wait_for(client, lambda: len(final_session(client)["segments"]) == 2)
            socket.send_json(stop_message(settings))
            assert assert_socket_closed(socket) == 1000
        wait_for(client, lambda: client.app.state.transcription.active_count == 0
                 and client.app.state.media_capture.active_count == 0)
        exported = client.get(f"/api/transcripts/{PARENT}/export?format=json")
        assert exported.status_code == 200
        session = exported.json()["session"]
        assert session["status"] == "completed" and session["ended_at"]
        assert {(item["track"], item["text"]) for item in session["segments"]} == {
            ("inbound", "Caller words."), ("outbound", "Playback words.")}
        assert all(item["start_ms"] == 0 and item["end_ms"] == 20 for item in session["segments"])
        assert session["tracks"]["inbound"]["interim"] == ""
        assert session["tracks"]["outbound"]["meaning"] == "caller-playback"
        plain = client.get(f"/api/transcripts/{PARENT}/export?format=txt")
        assert "[00:00] Caller input: Caller words." in plain.text
        assert "[00:00] Caller playback: Playback words." in plain.text
        assert "fixture-private-key" not in exported.text + plain.text
        saved = json.loads((tmp_path / "transcripts" / f"{PARENT}.json").read_text())
        assert saved["segments"] == session["segments"]
        assert all(track["frames"] == 1 for track in read_manifest(settings)["tracks"].values())
        assert client.app.state.switchboard.active_count == 1
        assert gateway.ended_calls == []  # Ending passive streams never ends the human call.


def test_word_endpoints_recover_noise_vad_but_preserve_continuing_speech(monkeypatch):
    """Replay the delayed-turn failure without a provider or a wall-clock wait."""
    clock = [10.0]
    monkeypatch.setattr("transcription.service.time.monotonic", lambda: clock[0])
    events = []
    manager = TranscriptionManager.__new__(TranscriptionManager)
    manager.settings = SimpleNamespace(media_max_seconds=1800)
    manager.revision = 0
    manager.on_segment = lambda sid, segment, final: events.append((dict(segment), final))
    session = _Session("CA" + "a" * 32, "MZ" + "b" * 32)
    manager.sessions = {session.call_sid: session}
    track = session.tracks["inbound"]
    track.offered_samples, track.close_sent = 8000 * 20, True

    class OneEvent:
        def __init__(self, event):
            self.event = event
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.event is None:
                raise StopAsyncIteration
            event, self.event = self.event, None
            return json.dumps(event)

    def words(text, start, end, *, final=True, speech_final=False):
        return {"type": "Results", "start": start, "duration": end - start,
            "is_final": final, "speech_final": speech_final,
            "channel": {"alternatives": [{"transcript": text, "confidence": .99,
                "words": [{"word": text, "start": start, "end": end - .1}]}]}}

    async def feed(event):
        await manager._receive(OneEvent(event), session, track)

    async def run():
        await feed(words("A complete question", 1, 2))
        await feed({"type": "SpeechStarted", "timestamp": 2.1})
        await feed({"type": "UtteranceEnd", "last_word_end": 1.9})
        assert events[-1][0]["endpoint_accepted"] is False
        assert events[-1][0]["endpoint_reason"] == "unconfirmed-vad-awaiting-quiet-audio"
        assert track.pending_utterance
        # Fresh quiet audio recovers a blank onset in a bounded interval.
        clock[0] = 11.6
        manager.offer(session.call_sid, "inbound", 20_000, b"\xff" * 160)
        assert manager._recover_quiet_turn(session, track)
        assert events[-1][0]["speech_final"] is True
        assert events[-1][0]["end_ms"] == 1900
        assert events[-1][0]["endpoint_source"] == "quiet-recovery"
        assert not track.pending_utterance and not track.speech_active

        # A new utterance's actual interim words invalidate the old endpoint.
        await feed(words("New finalized question", 3, 4))
        await feed(words("Still speaking", 4.2, 4.8, final=False))
        await feed({"type": "UtteranceEnd", "last_word_end": 3.9})
        assert events[-1][0]["endpoint_reason"] == "recognized-speech-after-boundary"
        clock[0] = 13.3
        track.last_audio_at = clock[0]
        assert not manager._recover_quiet_turn(session, track)
        assert track.pending_utterance and track.speech_active
        # A stale final cannot close the newer recognized words either.
        await feed(words("New finalized question", 3, 4, speech_final=True))
        assert events[-1][0]["endpoint_accepted"] is False
        # The old final's result window may extend beyond the new words; its
        # actual last word still cannot endpoint that newer speech.
        stale = words("New finalized question", 3, 6, speech_final=True)
        stale["channel"]["alternatives"][0]["words"][0]["end"] = 3.9
        await feed(stale)
        assert events[-1][0]["endpoint_accepted"] is False
        await feed(words("Still speaking", 4.2, 4.8))
        # Continuing acoustic speech and an audio outage never permit recovery.
        clock[0] = 15.0
        manager.offer(session.call_sid, "inbound", 20_020, b"\x80" * 160)
        assert not manager._recover_quiet_turn(session, track)
        clock[0] = 15.8
        assert not manager._recover_quiet_turn(session, track)
        manager.offer(session.call_sid, "inbound", 20_040, b"\xff" * 160)
        assert manager._recover_quiet_turn(session, track)
        assert events[-1][0]["last_word_end_ms"] == 4700
        await feed({"type": "UtteranceEnd", "last_word_end": -1})
        assert events[-1][0]["endpoint_reason"] == "already-finalized"
        assert not track.speech_active
        # Control diagnostics never enter the durable/exported segments.
        assert all("endpoint_source" not in item and "word_end_ms" not in item
                   for item in session.segments)

    asyncio.run(run())
