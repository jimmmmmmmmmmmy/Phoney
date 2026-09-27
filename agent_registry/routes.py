"""Agent configuration, optionally open for the demo. Saving never activates a call."""

import asyncio
import json
from pathlib import PurePath

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from .auth import (OWNER_COOKIE, SAFE_HEADERS, owner_authenticated, owner_write_access,
                   require_agent_origin)
from .provider import VoiceProvider
from .store import RegistryError, SESSION_SECONDS, text

MAX_JSON = 40_000
MAX_UPLOAD = 17 * 1024 * 1024
MAX_SAMPLE = 8 * 1024 * 1024
MAX_SAMPLES_TOTAL = 16 * 1024 * 1024
SAMPLE_MIME = {".wav": {"audio/wav", "audio/x-wav", "audio/wave"},
               ".mp3": {"audio/mpeg", "audio/mp3"}, ".m4a": {"audio/mp4", "audio/x-m4a"},
               ".flac": {"audio/flac", "audio/x-flac"}, ".ogg": {"audio/ogg"}}


async def bounded_body(request, maximum):
    length = request.headers.get("content-length")
    if length is not None and (not length.isascii() or not length.isdecimal() or len(length) > 10):
        raise HTTPException(400, "Invalid request length.", headers=SAFE_HEADERS)
    if length is not None and int(length) > maximum:
        raise HTTPException(413, "This upload is too large.", headers=SAFE_HEADERS)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > maximum:
            raise HTTPException(413, "This upload is too large.", headers=SAFE_HEADERS)
        data.extend(chunk)
    return bytes(data)


async def json_body(request):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "Use application/json.", headers=SAFE_HEADERS)

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    def bad_constant(_):
        raise ValueError

    try:
        data = json.loads(await bounded_body(request, MAX_JSON), object_pairs_hook=pairs, parse_constant=bad_constant)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, "Enter a valid JSON object.", headers=SAFE_HEADERS) from None


def _sample(filename, mime, data):
    if (not isinstance(filename, str) or not 1 <= len(filename) <= 128 or "/" in filename or "\\" in filename
            or any(ord(char) < 32 for char in filename)):
        raise HTTPException(400, "Use a simple audio filename.", headers=SAFE_HEADERS)
    suffix = PurePath(filename).suffix.lower()
    if suffix not in SAMPLE_MIME or mime not in SAMPLE_MIME[suffix]:
        raise HTTPException(415, "Upload WAV, MP3, M4A, FLAC, or OGG audio samples.", headers=SAFE_HEADERS)
    if not data or len(data) > MAX_SAMPLE:
        raise HTTPException(413, "Each voice sample must contain audio and be at most 8 MB.", headers=SAFE_HEADERS)
    valid = {".wav": data[:4] == b"RIFF" and data[8:12] == b"WAVE",
             ".mp3": data[:3] == b"ID3" or (len(data) > 1 and data[0] == 255 and data[1] & 0xe0 == 0xe0),
             ".m4a": data[4:8] == b"ftyp", ".flac": data[:4] == b"fLaC", ".ogg": data[:4] == b"OggS"}[suffix]
    if not valid:
        raise HTTPException(415, "The sample does not match its audio format.", headers=SAFE_HEADERS)
    return filename, data, mime


async def clone_form(request):
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data;"):
        raise HTTPException(415, "Upload voice samples with multipart/form-data.", headers=SAFE_HEADERS)
    # Bound the body before the multipart parser can spool attacker-sized files.
    body = await bounded_body(request, MAX_UPLOAD)
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    parsed = Request(request.scope, receive)
    async with parsed.form(max_files=3, max_fields=2) as form:
        if set(form) != {"name", "consent", "files"} or len(form.getlist("name")) != 1 or len(form.getlist("consent")) != 1:
            raise HTTPException(400, "Include a voice name, consent, and audio samples.", headers=SAFE_HEADERS)
        if form["consent"] != "true":
            raise HTTPException(400, "Confirm you own this voice or have the speaker’s permission to clone it.", headers=SAFE_HEADERS)
        try:
            name = text(form["name"], "voice name", 80)
        except RegistryError as exc:
            raise HTTPException(400, str(exc), headers=SAFE_HEADERS) from None
        files = form.getlist("files")
        if not 1 <= len(files) <= 3 or any(not isinstance(item, UploadFile) for item in files):
            raise HTTPException(400, "Upload one to three audio samples.", headers=SAFE_HEADERS)
        samples, total = [], 0
        for item in files:
            data = await item.read(MAX_SAMPLE + 1)
            total += len(data)
            if total > MAX_SAMPLES_TOTAL:
                raise HTTPException(413, "Voice samples must total at most 16 MB.", headers=SAFE_HEADERS)
            samples.append(_sample(item.filename, item.content_type, data))
        return name, samples


def register_agent_routes(app, settings, registry, voice_settings=None, *, provider=None):
    api_key = getattr(voice_settings, "elevenlabs_api_key", "")
    voice_provider = provider or (VoiceProvider(api_key) if api_key else None)
    provider_lock = asyncio.Lock()

    def enabled():
        return bool(getattr(settings, "agent_management_enabled", False) and registry and registry.enabled)

    def require_enabled():
        if not enabled():
            raise HTTPException(503, "Agent management is disabled or storage is not configured.", headers=SAFE_HEADERS)

    async def operation(method, *args, **kwargs):
        try:
            return await asyncio.to_thread(getattr(registry, method), *args, **kwargs)
        except RegistryError as exc:
            raise HTTPException(exc.status_code, str(exc), headers=SAFE_HEADERS) from None

    async def config(request, *, authenticated=None):
        authorized = enabled() and (await asyncio.to_thread(owner_authenticated, request, registry)
                                    if authenticated is None else authenticated)
        demo = enabled() and getattr(settings, "agent_demo_mode", False)
        result = {"authenticated": bool(authorized or demo), "demoMode": bool(demo),
                  "enabled": enabled(), "agents": [], "voices": [],
                  "manualEnabled": bool(getattr(settings, "voice_agent_enabled", False)),
                  "inboundEnabled": bool(getattr(settings, "operator_inbound_enabled", False)),
                  "automaticEnabled": bool(getattr(settings, "automatic_takeover_enabled", False)),
                  "capabilities": {"voiceCatalog": bool(voice_provider and enabled()), "voiceCloning": bool(voice_provider and enabled())}}
        if authorized or demo:
            result.update(await operation("snapshot"))
        return result

    @app.get("/api/agents/config")
    async def agent_config(request: Request):
        return JSONResponse(await config(request), headers=SAFE_HEADERS)

    @app.post("/api/agents/session")
    async def unlock(request: Request):
        require_enabled()
        require_agent_origin(request, settings)
        payload = await json_body(request)
        if set(payload) != {"code"}:
            raise HTTPException(400, "Enter your one-time owner access code.", headers=SAFE_HEADERS)
        token = await operation("consume_grant", payload["code"])
        response = JSONResponse(await config(request, authenticated=True), headers=SAFE_HEADERS)
        response.set_cookie(OWNER_COOKIE, token, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.delete("/api/agents/session")
    async def lock(request: Request):
        require_enabled()
        require_agent_origin(request, settings)
        await operation("revoke", request.cookies.get(OWNER_COOKIE))
        response = JSONResponse({"authenticated": False}, headers=SAFE_HEADERS)
        response.delete_cookie(OWNER_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.put("/api/agents/{agent_id}")
    async def publish_agent(agent_id: str, request: Request):
        require_enabled()
        if getattr(settings, "agent_demo_mode", False):
            require_agent_origin(request, settings)
        else:
            await asyncio.to_thread(owner_write_access, request, registry, settings)
        snapshot = await operation("publish", agent_id, await json_body(request), require_revision=True)
        return JSONResponse(snapshot.to_dict(), headers=SAFE_HEADERS)

    async def provider_action(method, *args):
        if voice_provider is None:
            raise HTTPException(503, "The voice provider is not configured.", headers=SAFE_HEADERS)
        if provider_lock.locked():
            raise HTTPException(409, "A voice request is already running. Wait for it to finish.", headers=SAFE_HEADERS)
        async with provider_lock:
            try:
                return await getattr(voice_provider, method)(*args)
            except RegistryError as exc:
                raise HTTPException(exc.status_code, str(exc), headers=SAFE_HEADERS) from None

    @app.post("/api/agents/voices/refresh")
    async def refresh_voices(request: Request):
        require_enabled()
        await asyncio.to_thread(owner_write_access, request, registry, settings)
        if await json_body(request) != {}:
            raise HTTPException(400, "Send an empty JSON object to refresh voices.", headers=SAFE_HEADERS)
        records = await provider_action("list_voices")
        return JSONResponse({"voices": await operation("replace_voices", records)}, headers=SAFE_HEADERS)

    @app.post("/api/agents/voices/clone")
    async def clone_voice(request: Request):
        require_enabled()
        await asyncio.to_thread(owner_write_access, request, registry, settings)
        if voice_provider is None:
            raise HTTPException(503, "The voice provider is not configured.", headers=SAFE_HEADERS)
        name, samples = await clone_form(request)
        record = await provider_action("clone", name, samples)
        voice = await operation("add_voice", record, consent=True)
        return JSONResponse(voice, status_code=201, headers=SAFE_HEADERS)

    return registry
