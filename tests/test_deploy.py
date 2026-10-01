"""Focused product and boundary checks; test helpers live in support."""

from unittest.mock import Mock, call

import pytest

from scripts import deploy

from support.deploy import NEW, OLD, drain_state, protected_app, supervisor


@pytest.mark.parametrize("port", [8000, 18000])
def test_failed_activation_rolls_back_previous_release(supervisor, monkeypatch, port):
    (supervisor.root / ".env").write_text(f"APP_PORT={port}\n")
    supervisor.save(active_commit=OLD)
    record = {"pid": 1234, "identity": "old process", "commit": OLD, "port": port}
    deploy.dev.write_state({"app": record, "ngrok": {"pid": 5678}, "public_url": "https://example.com"})
    monkeypatch.setattr(deploy.dev, "owned", lambda item: item == record)
    terminate = Mock()
    monkeypatch.setattr(deploy.dev, "terminate", terminate)
    launch = Mock(side_effect=[RuntimeError("new app failed"), None])
    monkeypatch.setattr(supervisor, "launch", launch)
    with pytest.raises(RuntimeError, match="new app failed"):
        supervisor.activate(supervisor.releases / NEW, NEW)
    assert launch.call_args_list[0].args[1] == NEW
    assert launch.call_args_list[1].args[1] == OLD
    assert supervisor.state["active_commit"] == OLD
    terminate.assert_called_once_with(record, timeout=40)


def test_activation_waits_for_both_calls_and_pending_cleanup(supervisor, monkeypatch):
    record, health, terminate, launch = protected_app(supervisor, monkeypatch)
    control = Mock(side_effect=[drain_state(active=1), drain_state(pending=2), drain_state()])
    monkeypatch.setattr(supervisor, "deployment_control", control)

    def pause(seconds):
        terminate.assert_not_called()
        launch.assert_not_called()

    monkeypatch.setattr(deploy.time, "sleep", pause)
    supervisor.activate(supervisor.releases / NEW, NEW)
    assert control.call_args_list == [call(True), call(), call()]
    terminate.assert_called_once_with(record, timeout=40)
    assert supervisor.state["active_commit"] == NEW
    health.assert_called_once_with()


@pytest.mark.parametrize("counts", [{"active": 1}, {"pending": 1}])
def test_drain_timeout_preserves_process_and_reopens_admission(supervisor, monkeypatch, counts):
    _, _, terminate, launch = protected_app(supervisor, monkeypatch)
    control = Mock(side_effect=[drain_state(**counts), drain_state(draining=False)])
    monkeypatch.setattr(supervisor, "deployment_control", control)
    monkeypatch.setattr(deploy.time, "monotonic", Mock(side_effect=[0, 61]))
    with pytest.raises(deploy.DeploymentDeferred, match="delayed deployment"):
        supervisor.activate(supervisor.releases / NEW, NEW)
    assert control.call_args_list == [call(True), call(False)]
    terminate.assert_not_called()
    launch.assert_not_called()
    assert supervisor.state["active_commit"] == OLD
    assert supervisor.state["drain_reset_required"] is False


def test_health_requires_exact_deployed_commit(supervisor, monkeypatch):
    monkeypatch.setattr(deploy.dev, "request", lambda url: {"status": "ok", "commit": OLD})
    assert supervisor.healthy(OLD)
    assert not supervisor.healthy(NEW)
