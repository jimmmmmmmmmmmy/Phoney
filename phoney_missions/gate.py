"""Safety flags for dialing. CI never places a call and never skips press 1."""

from __future__ import annotations

import os
from urllib.parse import urlsplit


def _env(environ):
    return os.environ if environ is None else environ


def live_call_block(base: str, environ=None) -> str | None:
    """Return a refusal reason, or None when a loopback dial may be posted."""
    env = _env(environ)
    if env.get("CI") or env.get("GITHUB_ACTIONS"):
        return "Refusing to place a call in CI."
    if env.get("PHONEY_ALLOW_LIVE_CALL") != "1":
        return "Set PHONEY_ALLOW_LIVE_CALL=1 on the Mac that should dial."
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return "Refusing to dial through a non-loopback URL."
    return None


def unattended_dial_permitted(environ=None, *, mission: bool) -> tuple[bool, str]:
    """Skip the owner press-1 gate only for a mission, with both flags, outside CI.

    Either flag alone keeps the owner ring. Non-mission calls always ring the owner.
    """
    env = _env(environ)
    if not mission:
        return False, "Owner press 1 remains required for non-mission calls."
    if env.get("CI") or env.get("GITHUB_ACTIONS"):
        return False, "Refusing unattended dial in CI."
    if env.get("PHONEY_UNATTENDED") != "1":
        return False, "PHONEY_UNATTENDED=1 is required to skip the owner press-1 gate."
    if env.get("PHONEY_ALLOW_LIVE_CALL") != "1":
        return False, "PHONEY_ALLOW_LIVE_CALL=1 is required together with PHONEY_UNATTENDED=1."
    return True, ""
