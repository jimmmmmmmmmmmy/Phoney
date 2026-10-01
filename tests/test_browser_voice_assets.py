"""The dashboard's real script URLs must be served, including the Voice SDK."""

import hashlib

import pytest

from test_dashboard import client_for
from test_dashboard_workspace import WorkspaceAssets


SDK_URL = "/assets/vendor/twilio-voice-sdk-2.18.5.min.js"


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


def test_voice_sdk_get_and_head_serve_the_pinned_bundle():
    with client_for() as client:
        asset = client.get(SDK_URL)
        assert asset.status_code == 200
        assert hashlib.sha256(asset.content).hexdigest() == (
            "35d3cb1b22e309f9884724a89250aecd2de4f1556ad7b67a7bc5e06c73dcb74a"
        )
        head = client.head(SDK_URL)
        assert head.status_code == 200 and not head.content
        assert head.headers["content-length"] == asset.headers["content-length"]


@pytest.mark.parametrize("path", [
    "/assets/vendor/twilio-voice-sdk-LICENSE.md",
    "/assets/vendor/twilio-voice-sdk-source.md",
    "/assets/vendor/other.js",
    "/assets/vendor/.env",
])
def test_vendor_route_only_serves_the_voice_sdk(path):
    with client_for() as client:
        assert client.get(path).status_code == 404
