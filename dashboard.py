"""Authenticated, read-only transcript views; provider credentials stay server-side."""

import base64
from collections import deque
from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import re
import time

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response


COOKIE = "operator_viewer"
SESSION_SECONDS = 8 * 60 * 60
SAFE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}
SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")


def _session_cookie(secret: str, expires: int) -> str:
    message = f"viewer-v1.{expires}"
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"{message}.{signature}"


def _valid_cookie(secret: str, value: str) -> bool:
    if not secret or len(value) > 160:
        return False
    try:
        prefix, expiry, signature = value.split(".")
        if prefix != "viewer-v1" or not expiry.isascii() or not expiry.isdecimal():
            return False
        expires = int(expiry)
        if not time.time() < expires <= time.time() + SESSION_SECONDS + 60:
            return False
        return hmac.compare_digest(value.encode(), _session_cookie(secret, expires).encode())
    except (ValueError, UnicodeError):
        return False


def register_dashboard(app, settings, manager):
    """Attach the HTML viewer and its private API without call-control capabilities."""
    failures = deque(maxlen=20)

    def auth(request: Request):
        if not settings.dashboard_token:
            raise HTTPException(503, "Transcript viewer is not configured", headers=SAFE_HEADERS)
        provided = request.headers.get("authorization", "")
        expected = "Bearer " + settings.dashboard_token
        if (provided and hmac.compare_digest(provided.encode(), expected.encode())):
            return
        if _valid_cookie(settings.dashboard_token, request.cookies.get(COOKIE, "")):
            return
        raise HTTPException(401, "Open the viewer with its access code", headers=SAFE_HEADERS)

    def same_origin(request):
        origin = request.headers.get("origin")
        local = {"http://localhost:8000", "http://127.0.0.1:8000"}
        if request.url.hostname in {"localhost", "127.0.0.1", "::1"}:
            local.add(f"{request.url.scheme}://{request.url.netloc}")
        if origin and origin not in {settings.public_base_url, *local}:
            raise HTTPException(403, "Cross-origin login is not allowed", headers=SAFE_HEADERS)

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
            + "; connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
              "frame-ancestors 'none'; form-action 'self'")
        return HTMLResponse(html, headers=headers)

    @app.post("/dashboard/login")
    async def login(request: Request):
        same_origin(request)
        if not settings.dashboard_token:
            raise HTTPException(503, "Transcript viewer is not configured", headers=SAFE_HEADERS)
        now = time.monotonic()
        while failures and now - failures[0] > 60:
            failures.popleft()
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 1024:
                raise HTTPException(413, "Login request is too large", headers=SAFE_HEADERS)
            body.extend(chunk)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Expected an access code", headers=SAFE_HEADERS) from None
        token = payload.get("token") if isinstance(payload, dict) else None
        if (not isinstance(token, str) or not hmac.compare_digest(
                token.encode(), settings.dashboard_token.encode())):
            if len(failures) >= 20:
                raise HTTPException(429, "Wait one minute before trying again", headers=SAFE_HEADERS)
            failures.append(now)
            raise HTTPException(401, "The access code is incorrect", headers=SAFE_HEADERS)
        response = JSONResponse({"status": "ok"}, headers=SAFE_HEADERS)
        response.set_cookie(COOKIE, _session_cookie(settings.dashboard_token, int(time.time()) + SESSION_SECONDS),
                            max_age=SESSION_SECONDS, httponly=True, samesite="strict",
                            secure=request.url.hostname not in {"localhost", "127.0.0.1", "::1"},
                            path="/")
        return response

    @app.post("/dashboard/logout")
    async def logout(request: Request):
        same_origin(request)
        response = JSONResponse({"status": "ok"}, headers=SAFE_HEADERS)
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
        return response

    @app.get("/api/transcripts", dependencies=[Depends(auth)])
    async def transcripts(call_sid: str | None = None):
        snapshot = deepcopy(manager.snapshot())
        snapshot["schema_version"] = 1
        sessions = snapshot["sessions"]
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

    @app.get("/api/transcripts/{call_sid}/export", dependencies=[Depends(auth)])
    async def export(call_sid: str, format: str = "json"):
        if not SID.fullmatch(call_sid) or format not in {"json", "txt"}:
            raise HTTPException(400, "Choose a valid call and json or txt format", headers=SAFE_HEADERS)
        session = next((s for s in manager.snapshot()["sessions"] if s["call_sid"] == call_sid), None)
        if session is None:
            raise HTTPException(404, "Transcript is not in the current history", headers=SAFE_HEADERS)
        headers = {**SAFE_HEADERS,
                   "Content-Disposition": f'attachment; filename="transcript-{call_sid}.{format}"'}
        if format == "json":
            data = {"schema_version": 1, "provider": "deepgram",
                    "model": session.get("model", settings.deepgram_model),
                    "sample_rate": 8000, "track_meanings": {
                        "inbound": "caller-input", "outbound": "caller-playback"},
                    "session": session}
            return JSONResponse(data, headers=headers)
        lines = ["New College Data Science Team — conversation transcript",
                 "Caller playback includes conference audio and prompts; it is not an isolated microphone.",
                 f"Call: {call_sid}", f"Status: {session['status']}", ""]
        for segment in session["segments"]:
            seconds = segment["start_ms"] // 1000
            label = "Caller input" if segment["track"] == "inbound" else "Caller playback"
            lines.append(f"[{seconds // 60:02d}:{seconds % 60:02d}] {label}: {segment['text']}")
        return Response("\n".join(lines) + "\n", media_type="text/plain", headers=headers)
