"""Owner cookies confer call-control authority; public workspace headers do not."""

from urllib.parse import urlsplit
from fastapi import HTTPException

from .store import RegistryError

OWNER_COOKIE = "__Host-operator-owner"
SAFE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or any(char.isspace() for char in value)):
            return None
        return parsed.hostname.lower(), parsed.port or 443
    except (TypeError, ValueError):
        return None


def owner_authenticated(request, registry):
    # The MVP's shared workspace grants the same authority to each trusted device.
    if getattr(getattr(request, "state", None), "workspace_authenticated", False):
        return True
    if registry is None:
        return False
    try:
        return registry.authorized(request.cookies.get(OWNER_COOKIE))
    except RegistryError:
        return False


def require_agent_origin(request, settings):
    allowed = {_origin(settings.public_base_url), _origin(str(request.base_url))} - {None}
    origins = request.headers.getlist("origin")
    if (len(origins) != 1 or _origin(origins[0]) not in allowed
            or request.headers.getlist("x-agent-request") != ["1"]):
        raise HTTPException(403, "Use the owner dashboard for this action.", headers=SAFE_HEADERS)


def owner_write_access(request, registry, settings):
    if not getattr(settings, "agent_management_enabled", False):
        raise HTTPException(503, "Agent management is disabled.", headers=SAFE_HEADERS)
    require_agent_origin(request, settings)
    if not owner_authenticated(request, registry):
        raise HTTPException(403, "Unlock owner controls before making this change.", headers=SAFE_HEADERS)
