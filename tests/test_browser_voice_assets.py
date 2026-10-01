"""Focused product and boundary checks; test helpers live in support."""

from support.browser_voice_assets import SDK_URL
from support.dashboard import client_for
from support.dashboard_workspace import WorkspaceAssets


def test_every_dashboard_script_url_is_served_as_javascript():
    with client_for() as client:
        page = client.get("/dashboard")
        parsed = WorkspaceAssets()
        parsed.feed(page.text)
        assert SDK_URL in parsed.scripts
        for path in parsed.scripts:
            assert path.startswith("/assets/"), path
            asset = client.get(path)
            assert asset.status_code == 200, path
            assert "javascript" in asset.headers["content-type"], path
            assert asset.headers["x-content-type-options"] == "nosniff", path
            assert asset.headers["cache-control"] == "no-store", path
            assert asset.content, path
