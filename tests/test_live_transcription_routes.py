"""Focused product and boundary checks; test helpers live in support."""

import json

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
