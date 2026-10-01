"""Shared fixtures and fakes for focused integration checks."""

from concurrent.futures import ThreadPoolExecutor


from workspace_store import WorkspaceConflict, WorkspaceStore


def contact(index=1, **changes):
    return {"id": f"local-contact-{index:08d}", "firstName": "Sample", "lastName": "Person",
            "phone": f"+1941666{index:04d}", "createdAt": "2026-09-26T15:00:00Z",
            "email": "sample@example.com", "address": "", "website": "", "company": "",
            "status": "New", "labels": ["Customers"], "demo": False, **changes}
