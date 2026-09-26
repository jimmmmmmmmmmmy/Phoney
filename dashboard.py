"""Public, read-only transcript views; provider credentials stay server-side."""

import base64
import asyncio
from copy import deepcopy
import hashlib
from pathlib import Path
import re

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response


SAFE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}
SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")


def register_dashboard(app, settings, manager, voicemail_store=None, recording_library=None,
                       call_details_store=None):
    """Attach a URL-accessible viewer and API without call-control capabilities."""
    def voicemail_snapshot():
        return (deepcopy(voicemail_store.snapshot()) if voicemail_store is not None else
                {"enabled": False, "storage_error": "", "voicemails": []})

    def recording_snapshot():
        return (recording_library.snapshot() if recording_library is not None else
                {"enabled": False, "storage_error": "", "recordings": []})

    @app.get("/dashboard", response_class=HTMLResponse)
    async def page():
        html = (Path(__file__).parent / "dashboard.html").read_text()
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        styles = re.findall(r"<style>(.*?)</style>", html, re.S)
        def hashes(blocks):
            return " ".join("'sha256-" + base64.b64encode(hashlib.sha256(
                block.encode()).digest()).decode() + "'" for block in blocks)
        headers = dict(SAFE_HEADERS)
        headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src " + hashes(scripts) + "; style-src " + hashes(styles)
            + "; connect-src 'self'; media-src 'self'; img-src 'self' data:; base-uri 'none'; "
              "frame-ancestors 'none'; form-action 'self'")
        return HTMLResponse(html, headers=headers)

    @app.get("/api/transcripts")
    async def transcripts(call_sid: str | None = None):
        snapshot = deepcopy(manager.snapshot())
        snapshot["schema_version"] = 1
        snapshot["voicemail"] = voicemail_snapshot()
        snapshot["recordings"] = await asyncio.to_thread(recording_snapshot)
        sessions = snapshot["sessions"]
        snapshot["call_details"] = (await asyncio.to_thread(call_details_store.snapshot, sessions)
                                    if call_details_store is not None else
                                    {"enabled": False, "storage_error": "", "calls": []})
        active = [s for s in sessions if not s.get("ended_at")]
        selected = next((s for s in sessions if s["call_sid"] == call_sid), None)
        if selected is None:
            selected = (active or sessions or [None])[0]
        snapshot["selected_call_sid"] = selected["call_sid"] if selected else None
        for session in sessions:
            if session is not selected:
                session["segments"] = []
                for track in session.get("tracks", {}).values():
                    track["interim"] = ""
        return JSONResponse(snapshot, headers=SAFE_HEADERS)

    @app.get("/api/voicemails")
    async def voicemails():
        return JSONResponse(voicemail_snapshot(), headers=SAFE_HEADERS)

    @app.get("/api/recordings")
    def recordings():
        return JSONResponse(recording_snapshot(), headers=SAFE_HEADERS)

    @app.api_route("/api/recordings/{call_sid}/audio", methods=["GET", "HEAD"])
    def recording_audio(call_sid: str, request: Request, track: str = "combined"):
        if recording_library is None:
            raise HTTPException(404, "Recording is unavailable", headers=SAFE_HEADERS)
        return recording_library.response(call_sid, track, request)

    @app.get("/api/transcripts/{call_sid}/export")
    async def export(call_sid: str, format: str = "json"):
        if not SID.fullmatch(call_sid) or format not in {"json", "txt"}:
            raise HTTPException(400, "Choose a valid call and json or txt format", headers=SAFE_HEADERS)
        session = next((s for s in manager.snapshot()["sessions"] if s["call_sid"] == call_sid), None)
        if session is None:
            raise HTTPException(404, "Transcript is not in the current history", headers=SAFE_HEADERS)
        voicemail = next((entry for entry in voicemail_snapshot()["voicemails"]
                          if entry["call_sid"] == call_sid), None)
        headers = {**SAFE_HEADERS,
                   "Content-Disposition": f'attachment; filename="transcript-{call_sid}.{format}"'}
        if format == "json":
            data = {"schema_version": 1, "provider": "deepgram",
                    "model": session.get("model", settings.deepgram_model),
                    "sample_rate": 8000, "track_meanings": {
                        "inbound": "caller-input", "outbound": "caller-playback"},
                    "session": session}
            if voicemail is not None:
                data["voicemail"] = voicemail
            if call_details_store is not None:
                details = await asyncio.to_thread(call_details_store.snapshot, [session])
                data["call_details"] = next((entry for entry in details["calls"]
                                             if entry["call_sid"] == call_sid), None)
            return JSONResponse(data, headers=headers)
        lines = ["New College Data Science Team — conversation transcript",
                 "Caller playback includes conference audio and prompts; it is not an isolated microphone.",
                 f"Call: {call_sid}", f"Status: {session['status']}"]
        if voicemail is not None:
            lines.append(f"Voicemail recording status: {voicemail['recording_status']}")
            if voicemail.get("duration_seconds") is not None:
                lines.append(f"Voicemail recording duration: {voicemail['duration_seconds']} seconds")
        lines.append("")
        for segment in session["segments"]:
            seconds = segment["start_ms"] // 1000
            label = "Caller input" if segment["track"] == "inbound" else "Caller playback"
            lines.append(f"[{seconds // 60:02d}:{seconds % 60:02d}] {label}: {segment['text']}")
        return Response("\n".join(lines) + "\n", media_type="text/plain", headers=headers)
