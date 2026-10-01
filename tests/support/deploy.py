"""Shared fixtures and fakes for focused integration checks."""

from unittest.mock import Mock, call


import pytest


from scripts import deploy


OLD = "1" * 40


NEW = "2" * 40


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    instance = deploy.Supervisor(tmp_path)
    monkeypatch.setattr(deploy.dev, "ROOT", tmp_path)
    monkeypatch.setattr(deploy.dev, "RUNTIME", tmp_path / ".runtime")
    monkeypatch.setattr(deploy.dev, "STATE", tmp_path / ".runtime/dev.json")
    monkeypatch.setattr(deploy.dev, "request", Mock(return_value=None))
    return instance


def protected_app(supervisor, monkeypatch):
    record = {"pid": 1234, "identity": "old process", "commit": OLD,
              "public_url": "https://operator.example"}
    supervisor.save(active_commit=OLD)
    deploy.dev.write_state({"app": record})
    alive = [True]
    monkeypatch.setattr(deploy.dev, "owned", lambda item: item == record and alive[0])
    health = Mock(return_value={"status": "ok", "build": 1, "switchboard_ready": True,
                                "commit": OLD})
    monkeypatch.setattr(supervisor, "health", health)
    terminate = Mock(side_effect=lambda item, timeout=5: alive.__setitem__(0, False))
    monkeypatch.setattr(deploy.dev, "terminate", terminate)
    launch = Mock()
    monkeypatch.setattr(supervisor, "launch", launch)
    return record, health, terminate, launch


def drain_state(active=0, pending=0, draining=True):
    return {"draining": draining, "active_sessions": active, "pending_work": pending}
