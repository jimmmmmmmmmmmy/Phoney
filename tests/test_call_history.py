"""Focused product and boundary checks; test helpers live in support."""

from call_history import PAGE_SIZE
from transcription import storage

from support.call_details import NUMBER
from support.call_history import document, seed, sid, viewer
from support.recording_playback import write_capture


def test_oldest_call_direct_transcript_summary_export_and_audio_after_restart(tmp_path):
    seed(tmp_path)
    write_capture(tmp_path / "recordings", sid=sid(1))
    client, manager, _, _ = viewer(tmp_path)
    assert sid(1) not in {item["call_sid"] for item in manager.snapshot()["sessions"]}
    with client:
        result = client.get("/api/transcripts", params={"call_sid": sid(1)}).json()
        assert result["selected_call_sid"] == sid(1)
        assert result["history"]["total"] == 45
        assert len(result["sessions"]) == PAGE_SIZE + 1
        chosen = next(item for item in result["sessions"] if item["call_sid"] == sid(1))
        assert chosen["segments"][0]["text"] == "Saved conversation 1."
        assert all(not row["segments"] for row in result["sessions"] if row is not chosen)
        details = next(item for item in result["call_details"]["calls"] if item["call_sid"] == sid(1))
        assert details["caller_number"] == NUMBER
        assert details["summary"]["text"] == "Summary 1"
        assert details["brief_summary"]["text"] == "Brief 1"
        exported = client.get(f"/api/transcripts/{sid(1)}/export").json()
        assert exported["call_details"]["summary"]["text"] == "Summary 1"
        assert exported["session"]["segments"] == chosen["segments"]
        assert "Saved conversation 1." in client.get(f"/api/transcripts/{sid(1)}/export?format=txt").text
        recording = next(item for item in result["recordings"]["recordings"] if item["call_sid"] == sid(1))
        assert client.get(recording["url"], headers={"Range": "bytes=0-43"}).status_code == 206
        assert len(manager.history) == storage.MAX_HISTORY


def test_keyset_cursor_survives_new_calls_and_equal_timestamps(tmp_path):
    for number in range(1, 42):
        storage.save(str(tmp_path / "transcripts"), document(number, minute=1))
    client, manager, _, _ = viewer(tmp_path)
    with client:
        first = client.get("/api/transcripts").json()
        storage.save(str(tmp_path / "transcripts"), document(99, minute=2))
        manager.archive._refreshed = float("-inf")
        second = client.get("/api/transcripts", params={"cursor": first["history"]["next_cursor"]}).json()
        third = client.get("/api/transcripts", params={"cursor": second["history"]["next_cursor"]}).json()
        assert [row["call_sid"] for result in (first, second, third) for row in result["sessions"]] == [sid(n) for n in range(41, 0, -1)]
