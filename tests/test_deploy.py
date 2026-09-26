"""Exercise deployment failure boundaries without starting services or contacting GitHub."""
import json
import os
import signal
import subprocess
from unittest.mock import Mock

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
    return instance


@pytest.mark.parametrize("value", ["../escape", "ABCDEF" * 7, "a" * 39, "a" * 41, None])
def test_rejects_unsafe_commit_paths(value):
    with pytest.raises(ValueError):
        deploy.validate_sha(value)
    assert deploy.validate_sha(NEW) == NEW


def test_duplicate_supervisor_is_refused(tmp_path):
    path = tmp_path / "lock"
    with deploy.exclusive_lock(path):
        with pytest.raises(RuntimeError, match="already running"):
            with deploy.exclusive_lock(path):
                pytest.fail("second owner entered")
    with deploy.exclusive_lock(path):
        pass


def test_failed_preparation_preserves_active_app_and_backs_off(supervisor, monkeypatch):
    supervisor.save(active_commit=OLD)
    monkeypatch.setattr(supervisor, "ensure_tunnel", Mock(return_value="https://operator.example"))
    monkeypatch.setattr(supervisor, "configure_hooks", Mock())
    monkeypatch.setattr(supervisor, "recover", Mock())
    monkeypatch.setattr(supervisor, "fetch", Mock(return_value=NEW))
    preparation = Mock(side_effect=RuntimeError("test failure"))
    activation = Mock()
    monkeypatch.setattr(supervisor, "prepare", preparation)
    monkeypatch.setattr(supervisor, "activate", activation)
    supervisor.check()
    assert supervisor.state["active_commit"] == OLD
    assert supervisor.state["failed_commit"] == NEW
    assert supervisor.state["last_error"] == "test failure"
    activation.assert_not_called()
    supervisor.check()
    assert preparation.call_count == 1
    supervisor.retry_path.touch()
    supervisor.check()
    assert preparation.call_count == 2


def test_fetch_failure_does_not_change_active_or_block_a_commit(supervisor, monkeypatch):
    supervisor.save(active_commit=OLD)
    monkeypatch.setattr(supervisor, "ensure_tunnel", Mock(return_value="https://operator.example"))
    monkeypatch.setattr(supervisor, "configure_hooks", Mock())
    recover = Mock()
    monkeypatch.setattr(supervisor, "recover", recover)
    monkeypatch.setattr(supervisor, "fetch", Mock(side_effect=RuntimeError("network unavailable")))
    supervisor.check()
    recover.assert_called_once()
    assert supervisor.state["active_commit"] == OLD
    assert supervisor.state.get("failed_commit") is None


def test_failed_activation_rolls_back_previous_release(supervisor, monkeypatch):
    supervisor.save(active_commit=OLD)
    record = {"pid": 1234, "identity": "old process", "commit": OLD}
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
    terminate.assert_called_once_with(record)


def test_activation_refuses_untracked_port(supervisor, monkeypatch):
    monkeypatch.setattr(deploy.dev, "owned", lambda record: False)
    monkeypatch.setattr(deploy.dev, "available", lambda port: False)
    terminate = Mock()
    monkeypatch.setattr(deploy.dev, "terminate", terminate)
    with pytest.raises(RuntimeError, match="untracked"):
        supervisor.activate(supervisor.releases / NEW, NEW)
    terminate.assert_not_called()


def test_same_commit_crash_is_recovered(supervisor, monkeypatch):
    supervisor.save(active_commit=OLD)
    monkeypatch.setattr(deploy.dev, "owned", lambda record: False)
    monkeypatch.setattr(deploy.dev, "terminate", Mock())
    launch = Mock()
    monkeypatch.setattr(supervisor, "launch", launch)
    supervisor.recover()
    launch.assert_called_once_with(supervisor.releases / OLD, OLD, {})
    assert supervisor.state["status"] == "running"


def test_environment_injects_local_secrets_only_at_runtime(supervisor, monkeypatch):
    (supervisor.root / ".env").write_text("TWILIO_AUTH_TOKEN=local-secret\nGITHUB_WEBHOOK_SECRET=hook-secret\n")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "inherited-secret")
    environment = supervisor.environment(NEW)
    assert environment["TWILIO_AUTH_TOKEN"] == "local-secret"
    assert environment["GITHUB_WEBHOOK_SECRET"] == "hook-secret"
    assert environment["DEPLOY_COMMIT"] == NEW
    assert environment["DEPLOY_TRIGGER_PATH"] == str(supervisor.trigger)
    assert "TWILIO_AUTH_TOKEN" not in supervisor.build_environment()
    assert "GITHUB_WEBHOOK_SECRET" not in supervisor.build_environment()
    (supervisor.root / ".env").write_text("DEPLOY_REPOSITORY=someone/another-repo\n")
    with pytest.raises(RuntimeError, match="DEPLOY_REPOSITORY"):
        supervisor.environment()


def test_health_requires_exact_deployed_commit(supervisor, monkeypatch):
    monkeypatch.setattr(deploy.dev, "request", lambda url: {"status": "ok", "commit": OLD})
    assert supervisor.healthy(OLD)
    assert not supervisor.healthy(NEW)


def test_state_files_and_runtime_are_private(supervisor):
    supervisor.save(active_commit=OLD)
    assert supervisor.state_path.stat().st_mode & 0o777 == 0o600
    assert supervisor.directory.stat().st_mode & 0o777 == 0o700
    assert json.loads(supervisor.state_path.read_text())["active_commit"] == OLD


def test_hook_configuration_only_runs_when_url_changes(supervisor, monkeypatch):
    command = Mock()
    monkeypatch.setattr(supervisor, "command", command)
    supervisor.configure_hooks("https://one.example")
    assert command.call_count == 2
    supervisor.configure_hooks("https://one.example")
    assert command.call_count == 2
    supervisor.configure_hooks("https://two.example")
    assert command.call_count == 4


def test_changed_tunnel_restarts_same_commit_with_new_environment(supervisor, monkeypatch):
    supervisor.save(active_commit=OLD)
    (supervisor.root / ".env").write_text("PUBLIC_BASE_URL=https://new.example\n")
    deploy.dev.write_state({"app": {"public_url": "https://old.example"}})
    monkeypatch.setattr(deploy.dev, "owned", lambda record: True)
    monkeypatch.setattr(supervisor, "healthy", lambda sha: True)
    monkeypatch.setattr(deploy.dev, "terminate", Mock())
    launch = Mock()
    monkeypatch.setattr(supervisor, "launch", launch)
    supervisor.recover()
    assert launch.call_args.args[1] == OLD


def test_shutdown_kills_the_build_process_group(supervisor, monkeypatch):
    process = Mock(pid=9876)
    process.poll.return_value = None

    def wait(timeout=None):
        if timeout is not None:
            supervisor.stopping = True
            raise subprocess.TimeoutExpired("build", timeout)
        return -9

    process.wait.side_effect = wait
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    kill = Mock()
    monkeypatch.setattr(os, "killpg", kill)
    with pytest.raises(deploy.DeploymentStopped):
        supervisor.command(["python", "-m", "pytest"])
    kill.assert_called_once_with(process.pid, signal.SIGKILL)
    assert process.wait.call_args.kwargs == {}


def test_shutdown_after_preparation_never_activates(supervisor, monkeypatch):
    monkeypatch.setattr(supervisor, "ensure_tunnel", Mock(return_value="https://operator.example"))
    monkeypatch.setattr(supervisor, "configure_hooks", Mock())
    monkeypatch.setattr(supervisor, "recover", Mock())
    monkeypatch.setattr(supervisor, "fetch", Mock(return_value=NEW))

    def prepare(sha):
        supervisor.stopping = True
        return supervisor.releases / sha

    monkeypatch.setattr(supervisor, "prepare", prepare)
    activate = Mock()
    monkeypatch.setattr(supervisor, "activate", activate)
    with pytest.raises(deploy.DeploymentStopped):
        supervisor.check()
    activate.assert_not_called()
    assert supervisor.state.get("failed_commit") is None
