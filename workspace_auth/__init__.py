"""Shared workspace access: real server sessions behind a mobile PIN screen."""

from .store import (AccessNotConfigured, AccessUnavailable, WorkspaceAccess,
                    SESSION_SECONDS, SHORT_SESSION_SECONDS)
from .web import install_workspace_access

__all__ = ["install_workspace_access", "WorkspaceAccess", "AccessUnavailable",
           "AccessNotConfigured", "SESSION_SECONDS", "SHORT_SESSION_SECONDS"]
