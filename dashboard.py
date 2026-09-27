"""Public dashboard views and shared workspace; credentials stay server-side."""

import base64
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from workspace_store import WorkspaceError


SAFE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}
SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
PUBLIC_DIRECTORY = Path(__file__).parent / "public"
RESUME_FILES = frozenset({"james-liu.pdf", "gerry-jones.pdf", "muhammed-altindal.pdf",
                          "shane-mccarthy.pdf"})
WORKSPACE_ASSETS = {
    "dashboard-workspace.js": "text/javascript",
    "dashboard-crm.js": "text/javascript",
    "dashboard-crm.css": "text/css",
    "dashboard-toolbar.js": "text/javascript",
    "dashboard-toolbar.css": "text/css",
    "dashboard-agents.js": "text/javascript",
    "dashboard-agents.css": "text/css",
}
MAX_WORKSPACE_BODY_BYTES = 2 * 1024 * 1024


def _workspace_origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, TypeError):
        return None


def html_page(filename):
    """Allow our same-origin assets and hash the HTML's inline scripts/styles."""
    html = (Path(__file__).parent / filename).read_text()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    styles = re.findall(r"<style>(.*?)</style>", html, re.S)

    def hashes(blocks):
        return " ".join("'sha256-" + base64.b64encode(hashlib.sha256(
            block.encode()).digest()).decode() + "'" for block in blocks) or "'none'"

    headers = dict(SAFE_HEADERS)
    headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self' " + hashes(scripts) + "; style-src 'self' " + hashes(styles)
        + "; connect-src 'self'; media-src 'self'; img-src 'self' data:; base-uri 'none'; "
          "frame-ancestors 'none'; form-action 'self'")
    return HTMLResponse(html, headers=headers)


def register_dashboard(app, settings, manager, voicemail_store=None, recording_library=None,
                       call_details_store=None, detection_store=None, workspace_store=None):
    """Attach a URL-accessible viewer and API without call-control capabilities."""
    async def workspace_payload(request):
        origins = request.headers.getlist("origin")
        allowed = {_workspace_origin(settings.public_base_url), _workspace_origin(str(request.base_url))}
        allowed.discard(None)
        if (len(origins) != 1 or _workspace_origin(origins[0]) not in allowed
                or request.headers.getlist("x-workspace-request") != ["1"]):
            raise HTTPException(403, "Workspace changes must come from this dashboard.", headers=SAFE_HEADERS)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "Use application/json for workspace changes.", headers=SAFE_HEADERS)
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal():
                raise HTTPException(400, "Invalid request length.", headers=SAFE_HEADERS)
            if len(length) > 10 or int(length) > MAX_WORKSPACE_BODY_BYTES:
                raise HTTPException(413, "Workspace request is too large.", headers=SAFE_HEADERS)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_WORKSPACE_BODY_BYTES:
                raise HTTPException(413, "Workspace request is too large.", headers=SAFE_HEADERS)
            body.extend(chunk)

        def unique_object(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result

        def invalid_constant(value):
            raise ValueError("Invalid JSON value")

        try:
            payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object,
                                 parse_constant=invalid_constant)
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Enter a valid JSON object.", headers=SAFE_HEADERS) from None

    async def workspace_operation(method, *args):
        if workspace_store is None:
            raise HTTPException(503, "Shared workspace storage is disabled.", headers=SAFE_HEADERS)
        try:
            result = await asyncio.to_thread(getattr(workspace_store, method), *args)
        except WorkspaceError as error:
            raise HTTPException(error.status_code, str(error), headers=SAFE_HEADERS) from None
        except Exception:
            raise HTTPException(503, "Workspace storage is unavailable. Changes were not saved.",
                                headers=SAFE_HEADERS) from None
        return JSONResponse(result, headers=SAFE_HEADERS)

    @app.get("/api/workspace")
    async def shared_workspace():
        return await workspace_operation("snapshot")

    @app.put("/api/workspace/contacts/{contact_id}")
    async def save_workspace_contact(contact_id: str, request: Request):
        return await workspace_operation("put_contact", contact_id, await workspace_payload(request))

    @app.put("/api/workspace/agents/{agent_id}")
    async def save_workspace_agent(agent_id: str, request: Request):
        return await workspace_operation("put_agent", agent_id, await workspace_payload(request))

    @app.post("/api/workspace/import")
    async def import_workspace(request: Request):
        return await workspace_operation("import_records", await workspace_payload(request))

    def voicemail_snapshot():
        return (deepcopy(voicemail_store.snapshot()) if voicemail_store is not None else
                {"enabled": False, "storage_error": "", "voicemails": []})

    def recording_snapshot():
        return (recording_library.snapshot() if recording_library is not None else
                {"enabled": False, "storage_error": "", "recordings": []})

    def detection_snapshot():
        if detection_store is None:
            return {"enabled": False, "storage_error": "", "calls": []}
        try:
            return detection_store.snapshot()
        except Exception:
            return {"enabled": True, "storage_error": "storage-unavailable", "calls": []}

    def detection_result(call_sid):
        if detection_store is None:
            return None
        try:
            return detection_store.get(call_sid)
        except Exception:
            return None

    @app.get("/dashboard", response_class=HTMLResponse)
    async def page():
        return html_page("dashboard.html")

    @app.get("/team")
    async def team_page():
        return RedirectResponse("/dashboard#team", status_code=307, headers=SAFE_HEADERS)

    @app.api_route("/assets/hacking-banyons.svg", methods=["GET", "HEAD"])
    def team_logo():
        logo = PUBLIC_DIRECTORY / "branding" / "hacking-banyons.svg"
        if not logo.is_file():
            raise HTTPException(404, "Logo is unavailable", headers=SAFE_HEADERS)
        return FileResponse(logo, media_type="image/svg+xml", headers=SAFE_HEADERS)

    @app.api_route("/assets/shellhacks-2026.png", methods=["GET", "HEAD"])
    def team_artwork():
        artwork = PUBLIC_DIRECTORY / "branding" / "shellhacks-2026.png"
        if not artwork.is_file():
            raise HTTPException(404, "Team artwork is unavailable", headers=SAFE_HEADERS)
        return FileResponse(artwork, media_type="image/png", headers=SAFE_HEADERS)

    @app.api_route("/assets/{filename}", methods=["GET", "HEAD"])
    def workspace_asset(filename: str):
        media_type = WORKSPACE_ASSETS.get(filename)
        asset = PUBLIC_DIRECTORY / filename
        if media_type is None or not asset.is_file():
            raise HTTPException(404, "Asset is unavailable", headers=SAFE_HEADERS)
        return FileResponse(asset, media_type=media_type, headers=SAFE_HEADERS)

    @app.api_route("/resumes/{filename}", methods=["GET", "HEAD"])
    def resume_pdf(filename: str):
        if filename not in RESUME_FILES:
            raise HTTPException(404, "Resume is unavailable", headers=SAFE_HEADERS)
        resume = PUBLIC_DIRECTORY / "resumes" / filename
        if not resume.is_file():
            raise HTTPException(404, "Resume is unavailable", headers=SAFE_HEADERS)
        headers = {**SAFE_HEADERS, "Content-Security-Policy": "default-src 'none'; sandbox"}
        return FileResponse(resume, media_type="application/pdf", filename=filename,
                            headers=headers)

    @app.get("/api/transcripts")
    async def transcripts(call_sid: str | None = None):
        snapshot = deepcopy(manager.snapshot())
        snapshot["schema_version"] = 1
        snapshot["voicemail"] = voicemail_snapshot()
        snapshot["recordings"] = await asyncio.to_thread(recording_snapshot)
        snapshot["detection"] = await asyncio.to_thread(detection_snapshot)
        sessions = snapshot["sessions"]
        snapshot["call_details"] = (await asyncio.to_thread(call_details_store.snapshot, sessions)
                                    if call_details_store is not None else
                                    {"enabled": False, "storage_error": "", "calls": []})
        active = [s for s in sessions if not s.get("ended_at")]
        selected = next((s for s in sessions if s["call_sid"] == call_sid), None)
        if selected is None:
            selected = (active or sessions or [None])[0]
        snapshot["selected_call_sid"] = selected["call_sid"] if selected else None
        # The list stays compact; only the selected call includes timed evidence.
        # Recording-only calls also need evidence even without a transcript session.
        evidence_sid = (call_sid if call_sid and SID.fullmatch(call_sid)
                        else snapshot["selected_call_sid"])
        if evidence_sid:
            evidence = await asyncio.to_thread(detection_result, evidence_sid)
            if evidence:
                catalog = snapshot["detection"]["calls"]
                existing = next((i for i, item in enumerate(catalog)
                                 if item["call_sid"] == evidence_sid), None)
                if existing is None:
                    catalog.append(evidence)
                else:
                    catalog[existing] = evidence
        detections = {result["call_sid"]: result for result in snapshot["detection"]["calls"]}
        for session in sessions:
            if session["call_sid"] in detections:
                session["detection"] = deepcopy(detections[session["call_sid"]])
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
            detection = await asyncio.to_thread(detection_result, call_sid)
            if detection is not None:
                data["detection"] = detection
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
            if segment.get("source") == "agent":
                label = f"Agent ({segment['speaker']})"
                if segment.get("delivery") == "interrupted":
                    label += " [interrupted]"
            lines.append(f"[{seconds // 60:02d}:{seconds % 60:02d}] {label}: {segment['text']}")
        return Response("\n".join(lines) + "\n", media_type="text/plain", headers=headers)
