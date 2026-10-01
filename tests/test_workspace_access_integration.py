"""Focused product and boundary checks; test helpers live in support."""

from fastapi.testclient import TestClient

from support.workspace_access_integration import BASE, configured_app


def test_private_pages_apis_downloads_and_unknown_routes_never_return_anonymous_data(configured_app):
    app, _ = configured_app
    sid = "CA" + "a" * 32
    paths = ["/dashboard", "/team", "/docs", "/openapi.json", "/api/workspace",
             "/api/notifications", "/api/transcripts", f"/api/transcripts/{sid}/export?format=json",
             f"/api/recordings/{sid}/audio", f"/api/voicemails/{sid}/audio",
             "/resumes/james-liu.pdf", "/assets/dashboard-workspace.js", "/api/future-private-route"]
    with TestClient(app, base_url=BASE.public_base_url) as client:
        for path in paths:
            response = client.get(path, follow_redirects=False, headers={"Range": "bytes=0-100"})
            assert response.status_code in {401, 303}, path
            assert "noindex" in response.headers["x-robots-tag"]
            assert response.headers["cache-control"] == "no-store"
            assert "<!doctype html>" not in response.text.lower()
        assert client.head(f"/api/voicemails/{sid}/audio").status_code == 401
        assert client.get("/unlock").status_code == 200
        assert client.get("/assets/workspace-unlock.js").status_code == 200
