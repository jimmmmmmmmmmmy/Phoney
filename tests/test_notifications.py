from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard import register_dashboard
from notification_store import NotificationStore
from tests.test_dashboard import Manager, SETTINGS, SID, OTHER, SAMPLE
from workspace_store import WorkspaceError, WorkspaceStore


def call(index, **changes):
    return {"call_sid": "CA" + f"{index:032x}", "started_at": f"2026-09-27T10:{index % 60:02d}:00Z",
            "caller_number": "+19415550101", **changes}


def test_notification_history_and_shared_read_state_survive_restart(tmp_path):
    path = str(tmp_path / "workspace")
    inbox = NotificationStore(WorkspaceStore(path))
    initial = inbox.observe([call(1), call(2, active=True)])
    assert len(initial["notifications"]) == 2
    assert [item["unread"] for item in initial["notifications"]] == [True, False]
    inbox.mark_read({"ids": [call(2)["call_sid"]]})
    restarted = NotificationStore(WorkspaceStore(path))
    saved = restarted.observe([call(1), call(2), call(3, voicemail=True)])
    assert [item["unread"] for item in saved["notifications"]] == [True, False, False]
    assert saved["notifications"][0]["collection"] == "voicemail"
    assert not saved["notifications"][1]["active"]


def test_acknowledgment_cannot_mark_a_newer_unseen_notification_read(tmp_path):
    inbox = NotificationStore(WorkspaceStore(str(tmp_path)))
    inbox.observe([])
    first = inbox.observe([call(1)])["notifications"]
    inbox.observe([call(1), call(2)])
    saved = inbox.mark_read({"ids": [first[0]["id"]]})["notifications"]
    assert [item["unread"] for item in saved] == [True, False]


def test_bounded_history_and_validation(tmp_path):
    workspace = WorkspaceStore(str(tmp_path))
    inbox = NotificationStore(workspace)
    assert len(inbox.observe([call(i) for i in range(180)])["notifications"]) == 20
    with workspace._transaction() as connection:
        assert connection.execute("SELECT count(*) FROM call_notifications").fetchone()[0] == 100
    for invalid in ({}, {"ids": ["bad"]}, {"ids": [None]}, {"ids": [] , "all": True}, {"ids": [SID] * 21}):
        with pytest.raises(WorkspaceError):
            inbox.mark_read(invalid)


def test_partial_archive_does_not_hide_history_but_confirmed_missing_call_is_not_live(tmp_path):
    inbox = NotificationStore(WorkspaceStore(str(tmp_path)))
    inbox.observe([call(1, active=True)])
    assert inbox.observe([], complete=False)["notifications"][0]["available"]
    missing = inbox.observe([])["notifications"][0]
    assert not missing["available"] and not missing["active"]
    restored = inbox.observe([call(1)])["notifications"][0]
    assert restored["available"] and not restored["active"]


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


def test_history_includes_calls_beyond_hot_cache(tmp_path):
    class ArchivedManager(Manager):
        def archive_snapshot(self):
            return {"sessions": [{**call(9), "ended_at": "2026-09-27T10:10:00Z"}]}
    app = FastAPI()
    register_dashboard(app, SETTINGS, ArchivedManager(), workspace_store=WorkspaceStore(str(tmp_path)))
    with TestClient(app) as client:
        entries = client.get("/api/notifications").json()["notifications"]
        assert {item["id"] for item in entries} == {SID, OTHER, call(9)["call_sid"]}
        assert entries[0]["available"] and not entries[0]["active"]


def test_live_alert_can_be_read_before_first_inbox_poll(tmp_path):
    app = FastAPI()
    register_dashboard(app, SETTINGS, Manager(), workspace_store=WorkspaceStore(str(tmp_path)))
    with TestClient(app, base_url=SETTINGS.public_base_url) as client:
        read = client.post("/api/notifications/read", json={"ids": [SID]}, headers={
            "Origin": SETTINGS.public_base_url, "X-Workspace-Request": "1"})
        assert read.status_code == 200
        assert not next(item for item in client.get("/api/notifications").json()["notifications"]
                        if item["id"] == SID)["unread"]
