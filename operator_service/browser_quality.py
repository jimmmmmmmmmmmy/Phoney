"""Private, bounded browser SDK quality observations; never part of call control.

The authenticated session supplies both Call SIDs. Browser reports contain only
allowlisted numeric metrics, coarse device settings and named SDK warnings.
Twilio's SDK reports jitter/RTT in milliseconds and packet loss as a percentage.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import Response

from agent_registry.auth import SAFE_HEADERS, require_agent_origin
from transcription.storage import private_directory
from .sessions import CALL_SID, OWNER, REMOTE

MAX_BODY_BYTES = 16000
MAX_FILE_BYTES = 64000
MAX_SAMPLES = 15
MAX_WARNINGS = 16
RETAIN_SAMPLES = 120
RETAIN_WARNINGS = 40
MAX_BATCHES = 1200
MIN_BATCH_SECONDS = 3
FINAL_GRACE_SECONDS = 120
MAX_ELAPSED_MS = 24 * 60 * 60 * 1000
METRICS = {
    "jitter": (0, 10000), "rtt": (0, 60000), "mos": (1, 5),
    "packetsLostFraction": (0, 100), "packetsLost": (0, 1000000),
    "packetsReceived": (0, 1000000), "packetsSent": (0, 1000000),
    "audioInputLevel": (0, 32767), "audioOutputLevel": (0, 32767),
}
WARNING_NAMES = frozenset({
    "high-jitter", "high-rtt", "low-mos", "high-packet-loss",
    "high-packets-lost-fraction", "constant-audio-input-level",
    "constant-audio-output-level", "low-bytes-received", "low-bytes-sent",
    "low-audio-input-level", "low-audio-output-level",
})


def _number(value, low, high):
    if (type(value) not in (int, float) or not low <= value <= high
            or not math.isfinite(value)):
        raise ValueError("Invalid quality metric")
    return value


def _elapsed(value):
    if type(value) is not int or not 0 <= value <= MAX_ELAPSED_MS:
        raise ValueError("Invalid observation time")
    return value


def validate_report(payload):
    if (not isinstance(payload, dict)
            or set(payload) != {"version", "sequence", "final", "elapsed_ms", "codec",
                                "device", "samples", "warnings"}
            or type(payload["version"]) is not int or payload["version"] != 1
            or type(payload["sequence"]) is not int or not 1 <= payload["sequence"] <= 10000
            or type(payload["final"]) is not bool
            or payload["codec"] not in (None, "opus", "pcmu")):
        raise ValueError("Invalid quality report")
    _elapsed(payload["elapsed_ms"])
    device = payload["device"]
    if not isinstance(device, dict) or set(device) - {
        "browser", "platform", "sdk_version", "audio_track_count", "sample_rate",
        "channel_count", "echo_cancellation", "noise_suppression", "auto_gain_control",
    }:
        raise ValueError("Invalid device metadata")
    for name, choices in {
        "browser": ("chrome", "edge", "firefox", "safari", "other"),
        "platform": ("mac", "windows", "linux", "ios", "android", "other"),
    }.items():
        if name in device and device[name] not in choices:
            raise ValueError("Invalid device metadata")
    if "sdk_version" in device and (not isinstance(device["sdk_version"], str)
            or not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,3}\.[0-9]{1,3}", device["sdk_version"])):
        raise ValueError("Invalid SDK version")
    for name, bounds in {"audio_track_count": (0, 16), "sample_rate": (8000, 192000),
                         "channel_count": (1, 8)}.items():
        if name in device:
            if type(device[name]) is not int:
                raise ValueError("Invalid device metadata")
            _number(device[name], *bounds)
    for name in ("echo_cancellation", "noise_suppression", "auto_gain_control"):
        if name in device and type(device[name]) is not bool:
            raise ValueError("Invalid device metadata")
    samples, warnings = payload["samples"], payload["warnings"]
    if not isinstance(samples, list) or len(samples) > MAX_SAMPLES:
        raise ValueError("Too many quality samples")
    for sample in samples:
        if (not isinstance(sample, dict) or "elapsed_ms" not in sample
                or set(sample) - (set(METRICS) | {"elapsed_ms"}) or len(sample) < 2):
            raise ValueError("Invalid quality sample")
        if _elapsed(sample["elapsed_ms"]) > payload["elapsed_ms"]:
            raise ValueError("Invalid observation time")
        for name, value in sample.items():
            if name in METRICS:
                _number(value, *METRICS[name])
    if not isinstance(warnings, list) or len(warnings) > MAX_WARNINGS:
        raise ValueError("Too many quality warnings")
    for warning in warnings:
        if (not isinstance(warning, dict) or set(warning) != {"elapsed_ms", "name", "cleared"}
                or warning["name"] not in WARNING_NAMES or type(warning["cleared"]) is not bool):
            raise ValueError("Invalid quality warning")
        if _elapsed(warning["elapsed_ms"]) > payload["elapsed_ms"]:
            raise ValueError("Invalid observation time")
    return payload


class QualityRateLimit(ValueError):
    pass


class BrowserQualityStore:
    """Atomic 0600 per-call summaries and a short tail of observations."""

    def __init__(self, settings, *, clock=time.time):
        directory = next((getattr(settings, name, "") for name in (
            "call_details_storage_dir", "transcript_storage_dir", "media_storage_dir",
            "workspace_storage_dir") if getattr(settings, name, "")), "")
        self.path = Path(directory) / "browser-quality" if directory else None
        self.enabled = self.path is not None and self.path.is_absolute()
        self.clock = clock
        self.lock = threading.Lock()

    def _load(self, sid):
        if not self.enabled or not self.path.exists():
            return None
        root = private_directory(self.path)
        try:
            try:
                fd = os.open(sid + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("Quality record is not a regular file")
                data = source.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("Quality record exceeds storage bound")
            document = json.loads(data)
            if not isinstance(document, dict) or document.get("call_sid") != sid:
                raise ValueError("Invalid quality record")
            return document
        finally:
            os.close(root)

    def load(self, call_sid):
        if not isinstance(call_sid, str) or not CALL_SID.fullmatch(call_sid):
            return None
        with self.lock:
            try:
                return self._load(call_sid)
            except (OSError, ValueError, TypeError):
                return None

    def record(self, call_sid, owner_call_sid, session_id, payload):
        validate_report(payload)
        if (not CALL_SID.fullmatch(call_sid) or not CALL_SID.fullmatch(owner_call_sid)
                or not re.fullmatch(r"[0-9a-f]{32}", session_id)):
            raise ValueError("Invalid server call identity")
        if not self.enabled:
            return False
        with self.lock:
            document = self._load(call_sid) or {
                "version": 1, "call_sid": call_sid, "owner_call_sid": owner_call_sid,
                "session_id": session_id, "last_sequence": 0, "batch_count": 0,
                "sample_count": 0, "metrics": {}, "samples": [], "warnings": [],
                "warning_counts": {}, "codec": None, "codec_changes": [], "final": False,
            }
            if document["session_id"] != session_id or document["owner_call_sid"] != owner_call_sid:
                raise ValueError("Quality record identity mismatch")
            if payload["sequence"] <= document["last_sequence"] or document["final"]:
                return False
            now = self.clock()
            if (document["batch_count"] >= MAX_BATCHES or (not payload["final"]
                    and now - document.get("last_received_at", 0) < MIN_BATCH_SECONDS)):
                raise QualityRateLimit("Quality report limit reached")
            document.update(last_sequence=payload["sequence"],
                            batch_count=document["batch_count"] + 1,
                            last_received_at=now, elapsed_ms=payload["elapsed_ms"],
                            device=payload["device"], final=payload["final"])
            codec = payload["codec"]
            if codec and codec != document["codec"]:
                document["codec"] = codec
                document["codec_changes"] = (document["codec_changes"] + [
                    {"elapsed_ms": payload["elapsed_ms"], "codec": codec}])[-16:]
            for sample in payload["samples"]:
                document["sample_count"] += 1
                for name, value in sample.items():
                    if name not in METRICS:
                        continue
                    metric = document["metrics"].setdefault(name,
                        {"count": 0, "sum": 0, "min": value, "max": value, "mean": value})
                    metric["count"] += 1
                    metric["sum"] += value
                    metric["min"] = min(metric["min"], value)
                    metric["max"] = max(metric["max"], value)
                    metric["mean"] = metric["sum"] / metric["count"]
            document["samples"] = (document["samples"] + payload["samples"])[-RETAIN_SAMPLES:]
            document["warnings"] = (document["warnings"] + payload["warnings"])[-RETAIN_WARNINGS:]
            for warning in payload["warnings"]:
                name = warning["name"] + (":cleared" if warning["cleared"] else ":raised")
                document["warning_counts"][name] = document["warning_counts"].get(name, 0) + 1
            self._save(call_sid, document)
            return True

    def _save(self, sid, document):
        data = json.dumps(document, ensure_ascii=True, allow_nan=False).encode()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Quality record exceeds storage bound")
        root = private_directory(self.path)
        temporary = f".{sid}.{secrets.token_hex(6)}.tmp"
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         mode=0o600, dir_fd=root)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, sid + ".json", src_dir_fd=root, dst_dir_fd=root)
        finally:
            try:
                os.unlink(temporary, dir_fd=root)
            except FileNotFoundError:
                pass
            os.close(root)


def register_browser_quality_routes(app, settings, sessions, *, require_owner):
    quality = BrowserQualityStore(settings)
    app.state.browser_quality = quality

    @app.post("/api/sessions/{session_id}/browser-quality")
    async def browser_quality(request: Request, session_id: str):
        require_agent_origin(request, settings)
        allowed = (await require_owner(request) if inspect.iscoroutinefunction(require_owner)
                   else await asyncio.to_thread(require_owner, request))
        if allowed is not True:
            raise HTTPException(403, "Owner authentication required", headers=SAFE_HEADERS)
        session = sessions.find(session_id)
        owner = session.legs.get(OWNER) if session else None
        if (not session or not session.browser_audio or not owner or owner.transport != "sdk"
                or not CALL_SID.fullmatch(owner.call_sid)):
            raise HTTPException(404, "Unknown browser call", headers=SAFE_HEADERS)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                raise HTTPException(413, "Quality report is too large", headers=SAFE_HEADERS)
            body.extend(chunk)
        try:
            payload = validate_report(json.loads(body))
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise HTTPException(400, "Invalid quality report", headers=SAFE_HEADERS) from None
        if (not session.active and (not payload["final"] or session.ended_at is None
                or time.monotonic() - session.ended_at > FINAL_GRACE_SECONDS)):
            raise HTTPException(409, "Browser call has ended", headers=SAFE_HEADERS)
        # Use the same remote/canonical identity as call history; no submitted SID.
        canonical = session.canonical_call_sid or session.legs[REMOTE].call_sid or owner.call_sid
        try:
            await asyncio.to_thread(quality.record, canonical, owner.call_sid, session.id, payload)
        except QualityRateLimit:
            raise HTTPException(429, "Quality report limit reached", headers=SAFE_HEADERS) from None
        except (OSError, ValueError, TypeError):
            # Diagnostics are best effort: a private disk failure must not affect calls.
            return Response(status_code=204, headers=SAFE_HEADERS)
        return Response(status_code=204, headers=SAFE_HEADERS)

    return quality
