"""Fail-closed ASGI gate, signed service exemptions, and same-origin unlock API."""

import asyncio
import json
import re
from urllib.parse import parse_qsl, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.datastructures import FormData, Headers
from twilio.request_validator import RequestValidator

from .store import (AccessNotConfigured, AccessUnavailable, SESSION_SECONDS,
                    WorkspaceAccess)


SESSION_COOKIE = "__Host-phoney-workspace"
DEVICE_COOKIE = "__Host-phoney-device"
SAFE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                "X-Robots-Tag": "noindex, nofollow, noarchive"}
MAX_BODY = 4096
PUBLIC_READ = {"/unlock", "/api/workspace-access/status", "/robots.txt",
               "/assets/workspace-unlock.js", "/assets/workspace-unlock.css"}
PUBLIC_WRITE = {"/api/workspace-access/unlock", "/api/workspace-access/logout"}
SERVICE_HTTP = {("GET", "/health"), ("HEAD", "/health"),
                ("GET", "/internal/deploy"), ("POST", "/internal/deploy"),
                ("POST", "/github/webhook")}
CALL_SID = r"CA[0-9a-fA-F]{32}"
OPERATOR_SID = r"[0-9a-f]{32}"
TWILIO_HTTP = re.compile(
    r"(?:/voice|/status|/(?:media/status|conference/events|calls/status|conference/finished|"
    r"voicemail|voicemail/finished|voicemail/recording)/" + CALL_SID
    + r"|/twilio/(?:status|reconnect)/" + OPERATOR_SID + r"/(?:owner|remote)"
    + r"|/twilio/voicemail/(?:finished|recording)/" + CALL_SID
    + r"|/twilio/native-agent|/twilio/native-agent/status/" + OPERATOR_SID + r"/[1-9][0-9]*"
    + r"|/twilio/native-(?:owner|menu|command|conference)/" + OPERATOR_SID
    + r"|/twilio/native-finished/" + OPERATOR_SID + r"/remote)\Z")
TWILIO_WS = re.compile(r"(?:/media/" + CALL_SID + r"/|/media/" + OPERATOR_SID
                       + r"/(?:owner|remote)/|/conference-media/" + OPERATOR_SID
                       + r"/(?:owner|remote)/|/native-agent-media/" + OPERATOR_SID + r"/)\Z")
BROWSER_WS = re.compile(r"/browser-media/" + OPERATOR_SID + r"/\Z")


def _https_origin(value):
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in {"", "/"} or url.query or url.fragment
                or any(c.isspace() for c in value)):
            return None
        return url.hostname.lower(), url.port or 443
    except (ValueError, TypeError):
        return None


def _client_ip(request):
    # Uvicorn may normalize this via a configured trusted proxy. Never accept
    # arbitrary X-Forwarded-For or Cloudflare headers directly from clients.
    return str(request.client.host if request.client else "unknown")[:128]


def _error(error, message, code=503, **extra):
    return JSONResponse({"error": error, "message": message, **extra},
                        status_code=code, headers=SAFE_HEADERS)


def _set_device(response, value):
    if value:
        response.set_cookie(DEVICE_COOKIE, value, secure=True, httponly=True,
                            samesite="lax", path="/", max_age=SESSION_SECONDS)


async def _json_payload(request, settings):
    origin = _https_origin(settings.public_base_url)
    origins = request.headers.getlist("origin")
    if (request.url.scheme != "https" or origin is None
            or len(origins) != 1 or _https_origin(origins[0]) != origin
            or request.headers.getlist("x-workspace-access") != ["1"]):
        return None, _error("request_rejected", "Unlock from the secure Phoney website.", 403)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return None, _error("request_rejected", "Use application/json.", 415)
    lengths = request.headers.getlist("content-length")
    if lengths and (len(lengths) != 1 or not lengths[0].isascii()
                    or not lengths[0].isdecimal() or len(lengths[0]) > 10
                    or int(lengths[0]) > MAX_BODY):
        return None, _error("request_rejected", "Unlock request is too large.", 413)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            return None, _error("request_rejected", "Unlock request is too large.", 413)
        body.extend(chunk)

    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Invalid constant")

    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique,
                             parse_constant=invalid_constant)
        if not isinstance(payload, dict):
            raise ValueError("Expected object")
        return payload, None
    except (ValueError, UnicodeError, RecursionError):
        return None, _error("request_rejected", "Enter a valid JSON object.", 400)


class WorkspaceGate:
    def __init__(self, app, access, settings):
        self.app = app
        self.access = access
        self.settings = settings
        self.enabled = getattr(settings, "workspace_access_enabled", False)

    async def _signed_twilio(self, scope, receive):
        """Bound the signed form body, then replay it to existing route checks."""
        headers = Headers(scope=scope)
        if (headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded"
                or len(headers.getlist("x-twilio-signature")) != 1):
            return False, receive
        body = bytearray()
        while True:
            chunk = await receive()
            if chunk["type"] != "http.request" or len(body) + len(chunk.get("body", b"")) > 64 * 1024:
                return False, receive
            body.extend(chunk.get("body", b""))
            if not chunk.get("more_body", False):
                break
        try:
            form = FormData(parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=100))
            path = scope.get("raw_path", b"/").decode("ascii")
            query = scope.get("query_string", b"").decode("ascii")
            url = self.settings.public_base_url + path + ("?" + query if query else "")
            valid = (form.getlist("AccountSid") == [self.settings.account_sid]
                     and RequestValidator(self.settings.auth_token).validate(
                         url, form, headers.get("x-twilio-signature", "")))
        except (ValueError, UnicodeError, AttributeError):
            valid = False
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return valid, replay

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)

        async def safe_send(message):
            if message["type"] == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() not in {b"x-robots-tag", b"cache-control", b"referrer-policy"}]
                headers.extend((key.lower().encode("ascii"), value.encode("ascii"))
                               for key, value in SAFE_HEADERS.items()
                               if key in {"X-Robots-Tag", "Cache-Control", "Referrer-Policy"})
                message = {**message, "headers": headers}
            await send(message)

        if not self.enabled:
            return await self.app(scope, receive, safe_send)
        path = scope.get("path", "")
        method = scope.get("method", "")
        if scope["type"] == "websocket":
            if TWILIO_WS.fullmatch(path):
                headers = Headers(scope=scope)
                try:
                    raw_path = scope.get("raw_path", b"/").decode("ascii")
                    valid = (not scope.get("query_string")
                             and len(headers.getlist("x-twilio-signature")) == 1
                             and RequestValidator(self.settings.auth_token).validate(
                                 self.settings.public_base_url.replace("https://", "wss://", 1) + raw_path,
                                 {}, headers.get("x-twilio-signature", "")))
                except (ValueError, UnicodeError, AttributeError):
                    valid = False
                if valid:
                    return await self.app(scope, receive, safe_send)
            if BROWSER_WS.fullmatch(path) and not scope.get("query_string"):
                headers = Headers(scope=scope)
                origins = headers.getlist("origin")
                request = Request({**scope, "type": "http", "method": "GET"})
                try:
                    authenticated = bool(self.access and len(origins) == 1
                        and _https_origin(origins[0]) == _https_origin(self.settings.public_base_url)
                        and await asyncio.to_thread(self.access.authenticated,
                                                    request.cookies.get(SESSION_COOKIE)))
                except (AccessNotConfigured, AccessUnavailable):
                    authenticated = False
                if authenticated:
                    scope.setdefault("state", {})["workspace_authenticated"] = True
                    scope["state"]["workspace_id"] = self.access.workspace_id
                    return await self.app(scope, receive, safe_send)
            return await send({"type": "websocket.close", "code": 1008})
        if ((method in {"GET", "HEAD"} and path in PUBLIC_READ)
                or (method == "POST" and path in PUBLIC_WRITE)
                or (method, path) in SERVICE_HTTP):
            return await self.app(scope, receive, safe_send)
        if method == "POST" and TWILIO_HTTP.fullmatch(path):
            valid, replay = await self._signed_twilio(scope, receive)
            if valid:
                return await self.app(scope, replay, safe_send)
            return await _error("request_rejected", "Invalid Twilio signature.", 403)(scope, receive, safe_send)
        request = Request(scope, receive=receive)
        if self.access is None:
            return await _error("unavailable", "Workspace access storage is unavailable.")(scope, receive, safe_send)
        try:
            authenticated = await asyncio.to_thread(self.access.authenticated,
                                                    request.cookies.get(SESSION_COOKIE))
        except AccessNotConfigured:
            return await _error("unconfigured", "Configure workspace access locally.")(scope, receive, safe_send)
        except AccessUnavailable:
            return await _error("unavailable", "Workspace access storage is unavailable.")(scope, receive, safe_send)
        if authenticated:
            scope.setdefault("state", {})["workspace_authenticated"] = True
            scope["state"]["workspace_id"] = self.access.workspace_id
            return await self.app(scope, receive, safe_send)
        if method in {"GET", "HEAD"} and not path.startswith(("/api/", "/assets/", "/resumes/")):
            response = RedirectResponse("/unlock", status_code=303, headers=SAFE_HEADERS)
        else:
            response = _error("authentication_required", "Unlock Phoney to continue.", 401)
        return await response(scope, receive, safe_send)


def install_workspace_access(app, settings):
    enabled = getattr(settings, "workspace_access_enabled", False)
    access = None
    if enabled:
        try:
            access = WorkspaceAccess(getattr(settings, "workspace_storage_dir", ""),
                                     getattr(settings, "database_url", ""),
                                     getattr(settings, "workspace_id", "default"))
        except (AccessUnavailable, OSError):
            # The gate still installs; a storage outage must never open a site.
            access = None
    app.state.workspace_access = access
    app.add_middleware(WorkspaceGate, access=access, settings=settings)

    @app.api_route("/unlock", methods=["GET", "HEAD"])
    async def unlock_page(request: Request):
        from dashboard import html_page
        if not enabled:
            return RedirectResponse("/dashboard", status_code=303, headers=SAFE_HEADERS)
        response = html_page("pin_unlock.html")
        if access:
            try:
                state, device = await asyncio.to_thread(access.status, request.cookies.get(DEVICE_COOKIE),
                                                        _client_ip(request), request.cookies.get(SESSION_COOKIE))
                if state["authenticated"]:
                    return RedirectResponse("/dashboard", status_code=303, headers=SAFE_HEADERS)
                _set_device(response, device)
            except (AccessUnavailable, AccessNotConfigured):
                pass
        return response

    @app.get("/robots.txt")
    async def robots():
        # Permit crawlers to read the unlock page's noindex header. Disallowing
        # it would hide that directive while still allowing URL-only listings.
        return PlainTextResponse("User-agent: *\nDisallow:\n", headers=SAFE_HEADERS)

    @app.get("/api/workspace-access/status")
    async def status(request: Request):
        if not enabled:
            return JSONResponse({"enabled": False, "configured": False, "authenticated": True,
                                 "password_required": False, "attempts_remaining": 3}, headers=SAFE_HEADERS)
        if access is None:
            return _error("unavailable", "Workspace access storage is unavailable.")
        try:
            result, device = await asyncio.to_thread(access.status, request.cookies.get(DEVICE_COOKIE),
                                                     _client_ip(request), request.cookies.get(SESSION_COOKIE))
        except AccessNotConfigured:
            return _error("unconfigured", "Configure the PIN and recovery password locally.",
                          enabled=True, configured=False, authenticated=False)
        except AccessUnavailable:
            return _error("unavailable", "Workspace access storage is unavailable.")
        response = JSONResponse(result, headers=SAFE_HEADERS)
        _set_device(response, device)
        return response

    @app.post("/api/workspace-access/unlock")
    async def unlock(request: Request):
        payload, error = await _json_payload(request, settings)
        if error:
            return error
        if not enabled or access is None:
            return _error("unavailable", "Workspace access is unavailable.")
        if (set(payload) - {"method", "credential", "remember"}
                or not isinstance(payload.get("method"), str)
                or payload.get("method") not in {"pin", "password"}
                or not isinstance(payload.get("credential"), str)
                or any(0xD800 <= ord(c) <= 0xDFFF for c in payload.get("credential", ""))
                or type(payload.get("remember", True)) is not bool):
            return _error("request_rejected", "Choose PIN or password and enter a credential.", 400)
        try:
            code, result, token, device = await asyncio.to_thread(
                access.unlock, request.cookies.get(DEVICE_COOKIE), _client_ip(request),
                payload["method"], payload["credential"], payload.get("remember", True))
        except AccessNotConfigured:
            return _error("unconfigured", "Configure the PIN and recovery password locally.")
        except AccessUnavailable:
            return _error("unavailable", "Workspace access storage is unavailable.")
        headers = dict(SAFE_HEADERS)
        if result.get("retry_after"):
            headers["Retry-After"] = str(result["retry_after"])
        response = JSONResponse(result, status_code=code, headers=headers)
        _set_device(response, device)
        if token:
            response.set_cookie(SESSION_COOKIE, token, secure=True, httponly=True, samesite="lax",
                                path="/", max_age=SESSION_SECONDS if payload.get("remember", True) else None)
        return response

    @app.post("/api/workspace-access/logout")
    async def logout(request: Request):
        payload, error = await _json_payload(request, settings)
        if error:
            return error
        if payload:
            return _error("request_rejected", "Use an empty JSON object to lock this device.", 400)
        if not enabled or access is None:
            return _error("unavailable", "Workspace access is unavailable.")
        try:
            await asyncio.to_thread(access.revoke, request.cookies.get(SESSION_COOKIE, ""))
        except AccessUnavailable:
            return _error("unavailable", "Workspace access storage is unavailable.")
        response = JSONResponse({"authenticated": False, "redirect": "/unlock"}, headers=SAFE_HEADERS)
        response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
        return response

    return access
