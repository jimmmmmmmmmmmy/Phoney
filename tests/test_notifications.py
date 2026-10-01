"""Focused product and boundary checks; test helpers live in support."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard import register_dashboard
from workspace_store import WorkspaceStore

from support.dashboard import Manager, SETTINGS, SID


def test_routes_persist_across_origin_and_reject_cross_site_acknowledgment(tmp_path):
    def client(origin):
        app = FastAPI()
        register_dashboard(app, SETTINGS, Manager(), workspace_store=WorkspaceStore(str(tmp_path)))
        return TestClient(app, base_url=origin)
    with client("https://one.example") as first:
        response = first.get("/api/notifications")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["notifications"][0]["id"] == SID
        for headers in ({}, {"Origin": "https://foreign.example", "X-Workspace-Request": "1"}):
            assert first.post("/api/notifications/read", json={"ids": [SID]}, headers=headers).status_code == 403
        assert first.post("/api/notifications/read", json={"ids": [SID]}, headers={
            "Origin": "https://one.example", "X-Workspace-Request": "1"}).status_code == 200
    with client("https://new-tunnel.example") as second:
        data = second.get("/api/notifications").json()
        assert len(data["notifications"]) == 2
        assert not any(item["unread"] for item in data["notifications"])
        assert all("segments" not in item for item in data["notifications"])
