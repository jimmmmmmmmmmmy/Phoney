"""Exercise deployment failure boundaries without starting services or contacting GitHub."""
import json
import io
import os
import signal
import socket
import subprocess
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
    terminate.assert_called_once_with(record, timeout=40)


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
    monkeypatch.setattr(supervisor, "health", Mock(return_value={"status": "ok", "commit": OLD}))
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


def test_available_refuses_an_active_listener():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert not deploy.dev.available(listener.getsockname()[1])


def test_available_allows_rebind_after_server_closes_connection():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        address = listener.getsockname()
        listener.listen()
        with socket.create_connection(address, timeout=2) as client:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                # Server sends the first FIN, leaving its port in TIME_WAIT.
                connection.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
                client.shutdown(socket.SHUT_WR)
                assert connection.recv(1) == b""
    assert deploy.dev.available(address[1])


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


def test_shutdown_during_drain_reopens_admission(supervisor, monkeypatch):
    _, _, terminate, launch = protected_app(supervisor, monkeypatch)

    def control(draining=None):
        if draining is True:
            supervisor.stopping = True
        return drain_state(active=1, draining=draining is not False)

    monkeypatch.setattr(supervisor, "deployment_control", Mock(side_effect=control))
    with pytest.raises(deploy.DeploymentStopped):
        supervisor.activate(supervisor.releases / NEW, NEW)
    assert supervisor.deployment_control.call_args_list == [call(True), call(False)]
    terminate.assert_not_called()
    launch.assert_not_called()


def test_failed_termination_reopens_surviving_process(supervisor, monkeypatch):
    _, _, terminate, launch = protected_app(supervisor, monkeypatch)
    terminate.side_effect = RuntimeError("cannot signal")
    control = Mock(side_effect=[drain_state(), drain_state(draining=False)])
    monkeypatch.setattr(supervisor, "deployment_control", control)
    with pytest.raises(RuntimeError, match="cannot signal"):
        supervisor.activate(supervisor.releases / NEW, NEW)
    assert control.call_args_list == [call(True), call(False)]
    launch.assert_not_called()


def test_deferred_activation_is_retried_without_failed_commit(supervisor, monkeypatch):
    supervisor.save(active_commit=OLD)
    monkeypatch.setattr(supervisor, "ensure_tunnel", Mock(return_value="https://operator.example"))
    monkeypatch.setattr(supervisor, "configure_hooks", Mock())
    monkeypatch.setattr(supervisor, "recover", Mock())
    monkeypatch.setattr(supervisor, "fetch", Mock(return_value=NEW))
    preparation = Mock(return_value=supervisor.releases / NEW)
    monkeypatch.setattr(supervisor, "prepare", preparation)
    monkeypatch.setattr(supervisor, "activate", Mock(side_effect=deploy.DeploymentDeferred("Call in progress.")))
    supervisor.check()
    assert supervisor.state["status"] == "waiting"
    assert supervisor.state["active_commit"] == OLD
    assert supervisor.state.get("failed_commit") is None
    supervisor.check()
    assert preparation.call_count == 2


@pytest.mark.parametrize("health", [{"build": 0}, {"build": 1, "switchboard_ready": False}, None])
def test_non_switchboard_activation_does_not_require_drain(supervisor, monkeypatch, health):
    _, health_read, terminate, _ = protected_app(supervisor, monkeypatch)
    health_read.return_value = health
    control = Mock()
    monkeypatch.setattr(supervisor, "deployment_control", control)
    supervisor.activate(supervisor.releases / NEW, NEW)
    control.assert_not_called()
    terminate.assert_called_once()


def test_tunnel_recovery_preserves_active_calls_and_only_reads_health_once(supervisor, monkeypatch):
    _, health, terminate, launch = protected_app(supervisor, monkeypatch)
    (supervisor.root / ".env").write_text("PUBLIC_BASE_URL=https://new.example\n")
    monkeypatch.setattr(supervisor, "deployment_control", Mock(side_effect=[
        drain_state(active=1), drain_state(draining=False)]))
    monkeypatch.setattr(deploy.time, "monotonic", Mock(side_effect=[0, 61]))
    with pytest.raises(deploy.DeploymentDeferred):
        supervisor.recover()
    health.assert_called_once_with()
    terminate.assert_not_called()
    launch.assert_not_called()


def test_failed_drain_reset_is_retried_before_leaving_healthy_app_running(supervisor, monkeypatch):
    _, _, terminate, launch = protected_app(supervisor, monkeypatch)
    (supervisor.root / ".env").write_text("PUBLIC_BASE_URL=https://operator.example\n")
    supervisor.save(drain_reset_required=True)
    control = Mock(return_value=drain_state(draining=False))
    monkeypatch.setattr(supervisor, "deployment_control", control)
    supervisor.recover()
    control.assert_called_once_with(False)
    assert supervisor.state["drain_reset_required"] is False
    terminate.assert_not_called()
    launch.assert_not_called()


def test_control_uses_local_bearer_and_explicit_json_body(supervisor, monkeypatch):
    (supervisor.root / ".env").write_text("DEPLOY_CONTROL_TOKEN=private-deployment-secret\n")
    response = io.StringIO(json.dumps(drain_state()))
    opener = Mock(return_value=response)
    monkeypatch.setattr(deploy, "urlopen", opener)
    assert supervisor.deployment_control(True) == drain_state()
    request = opener.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:8000/internal/deploy"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer private-deployment-secret"
    assert json.loads(request.data) == {"draining": True}
    assert opener.call_args.kwargs == {"timeout": 2}


@pytest.mark.parametrize("response", [{}, {"draining": True, "active_sessions": 0},
    drain_state(active=-1), drain_state(pending=True), drain_state(draining=False)])
def test_invalid_drain_state_cannot_authorize_replacement(supervisor, monkeypatch, response):
    (supervisor.root / ".env").write_text("DEPLOY_CONTROL_TOKEN=private-deployment-secret\n")
    monkeypatch.setattr(deploy, "urlopen", Mock(return_value=io.StringIO(json.dumps(response))))
    with pytest.raises(deploy.DeploymentDeferred, match="invalid deployment state"):
        supervisor.deployment_control(True)


def test_missing_control_token_preserves_protected_app(supervisor, monkeypatch):
    _, _, terminate, launch = protected_app(supervisor, monkeypatch)
    monkeypatch.setattr(supervisor, "environment", lambda: {})
    opener = Mock()
    monkeypatch.setattr(deploy, "urlopen", opener)
    with pytest.raises(deploy.DeploymentDeferred, match="DEPLOY_CONTROL_TOKEN"):
        supervisor.activate(supervisor.releases / NEW, NEW)
    opener.assert_not_called()
    terminate.assert_not_called()
    launch.assert_not_called()


def test_dev_stop_allows_app_cleanup_but_keeps_tunnel_timeout_short(supervisor, monkeypatch):
    app, ngrok, cloudflared = {"pid": 1234}, {"pid": 5678}, {"pid": 9012}
    deploy.dev.write_state({"app": app, "ngrok": ngrok, "cloudflared": cloudflared})
    terminate = Mock()
    monkeypatch.setattr(deploy.dev, "terminate", terminate)
    deploy.dev.stop()
    assert terminate.call_args_list == [call(app, timeout=40), call(ngrok, timeout=5),
                                         call(cloudflared, timeout=5)]


def test_ambiguous_drain_request_reopens_admission(supervisor, monkeypatch):
    _, _, terminate, launch = protected_app(supervisor, monkeypatch)
    control = Mock(side_effect=[deploy.DeploymentDeferred("request timed out"),
                               drain_state(draining=False)])
    monkeypatch.setattr(supervisor, "deployment_control", control)
    with pytest.raises(deploy.DeploymentDeferred, match="request timed out"):
        supervisor.activate(supervisor.releases / NEW, NEW)
    assert control.call_args_list == [call(True), call(False)]
    assert supervisor.state["drain_reset_required"] is False
    terminate.assert_not_called()
    launch.assert_not_called()


def test_failed_admission_reset_remains_visible_for_recovery(supervisor, monkeypatch):
    _, _, terminate, _ = protected_app(supervisor, monkeypatch)
    control = Mock(side_effect=[deploy.DeploymentDeferred("request timed out"),
                               deploy.DeploymentDeferred("reset unavailable")])
    monkeypatch.setattr(supervisor, "deployment_control", control)
    with pytest.raises(deploy.DeploymentDeferred, match="request timed out"):
        supervisor.activate(supervisor.releases / NEW, NEW)
    assert supervisor.state["drain_reset_required"] is True
    terminate.assert_not_called()


def test_supervisor_selects_configured_tunnel_and_propagates_shutdown(supervisor, monkeypatch):
    (supervisor.root / ".env").write_text("TUNNEL_PROVIDER=cloudflare\n")
    helper = Mock(return_value="https://demo.trycloudflare.com")
    monkeypatch.setattr(deploy.dev, "ensure_tunnel", helper)
    assert supervisor.ensure_tunnel() == "https://demo.trycloudflare.com"
    assert helper.call_args.args[0]["TUNNEL_PROVIDER"] == "cloudflare"
    stopping = helper.call_args.kwargs["stopping"]
    stopping()
    supervisor.stopping = True
    with pytest.raises(deploy.DeploymentStopped):
        stopping()


def test_tunnel_recovery_reconfigures_webhooks_before_checking_github(supervisor, monkeypatch):
    supervisor.save(active_commit=OLD, configured_public_url="https://old.example")
    calls = []
    new_url = "https://new.trycloudflare.com"
    monkeypatch.setattr(supervisor, "ensure_tunnel", lambda: new_url)
    monkeypatch.setattr(supervisor, "recover", lambda: calls.append("recover"))
    monkeypatch.setattr(supervisor, "command", lambda args, **kwargs: calls.append(args[1].name))
    def fetch():
        assert supervisor.state["configured_public_url"] == new_url
        calls.append("fetch")
        return OLD
    monkeypatch.setattr(supervisor, "fetch", fetch)
    supervisor.check()
    assert calls == ["recover", "configure_twilio.py", "configure_github.py", "fetch"]
    assert supervisor.state["status"] == "running"


@pytest.mark.parametrize("preparation_fails", [False, True])
def test_first_deployment_configures_new_webhooks_only_after_activation(supervisor, monkeypatch, preparation_fails):
    new_url = "https://new.trycloudflare.com"
    calls = []
    monkeypatch.setattr(supervisor, "ensure_tunnel", lambda: new_url)
    monkeypatch.setattr(supervisor, "recover", Mock())
    monkeypatch.setattr(supervisor, "fetch", lambda: NEW)
    def prepare(sha):
        if preparation_fails:
            raise RuntimeError("failed build")
        return supervisor.releases / sha
    monkeypatch.setattr(supervisor, "prepare", prepare)
    monkeypatch.setattr(supervisor, "activate", lambda *args: calls.append("activate"))
    monkeypatch.setattr(supervisor, "configure_hooks", lambda url: calls.append("hooks"))
    supervisor.check()
    assert calls == ([] if preparation_fails else ["activate", "hooks"])


def test_provider_switch_waits_for_calls_before_replacing_connector(supervisor, monkeypatch):
    protected_app(supervisor, monkeypatch)
    (supervisor.root / ".env").write_text("TUNNEL_PROVIDER=cloudflare\nDEPLOY_CONTROL_TOKEN=" + "x" * 32 + "\n")
    helper = Mock()
    monkeypatch.setattr(deploy.dev, "ensure_tunnel", helper)
    control = Mock(side_effect=[deploy.DeploymentDeferred("call still active"), drain_state(draining=False)])
    monkeypatch.setattr(supervisor, "deployment_control", control)
    with pytest.raises(deploy.DeploymentDeferred):
        supervisor.ensure_tunnel()
    helper.assert_not_called()
    assert control.call_args_list == [call(True), call(False)]
