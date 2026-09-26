import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import dev


URL = "https://current-demo.trycloudflare.com"
OLD_URL = "https://previous-demo.trycloudflare.com"


def write_log_record(path, prefix=b"", content=None, pid=123):
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(prefix + (content if content is not None else (URL + "\n").encode()))
    info = path.stat()
    return {"pid": pid, "identity": "owned", "log_offset": len(prefix),
            "log_inode": info.st_ino, "log_device": info.st_dev}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "RUNTIME", tmp_path / ".runtime")
    monkeypatch.setattr(dev, "STATE", tmp_path / ".runtime/dev.json")
    monkeypatch.setattr(dev, "owned", lambda record: bool(record and record.get("identity") == "owned"))
    monkeypatch.setattr(dev, "available", lambda port: True)
    monkeypatch.setattr(dev.shutil, "which", lambda name: "/test/" + name)
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(dev, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    fake = SimpleNamespace(url=URL, ready=True, launched=[], stopped=[], ngrok_url=None, emit_url=True)

    def listeners(port):
        record = dev.read_state().get("cloudflared")
        return {record["pid"]} if dev.owned(record) else set()

    def request(url):
        if url == dev.CLOUDFLARE_READY and fake.ready:
            return {"status": 200, "readyConnections": 1}
        if url == dev.TUNNELS and fake.ngrok_url:
            return {"tunnels": [{"config": {"addr": dev.BASE}, "public_url": fake.ngrok_url}]}
        return None

    def spawn(name, command, state, environment=None):
        fake.launched.append((name, command, environment))
        if name == "cloudflared":
            log = dev.RUNTIME / "cloudflared.log"
            prefix = log.read_bytes() if log.exists() else b""
            content = (fake.url + "\n").encode() if fake.emit_url else b""
            record = write_log_record(log, prefix, content)
        else:
            record = {"pid": 456, "identity": "owned"}
            fake.ngrok_url = "https://example.ngrok-free.app"
        state[name] = record
        dev.write_state(state)
        return SimpleNamespace(pid=record["pid"], poll=lambda: None)

    monkeypatch.setattr(dev, "listener_pids", listeners)
    monkeypatch.setattr(dev, "request", request)
    monkeypatch.setattr(dev, "spawn", spawn)
    monkeypatch.setattr(dev, "terminate", lambda record, timeout=5: fake.stopped.append((record, timeout)))
    return fake


def test_cloudflare_starts_preserves_app_and_persists_private_state(sandbox):
    app = {"pid": 900, "identity": "app", "commit": "abc"}
    dev.write_state({"app": app, "public_url": OLD_URL})
    (dev.ROOT / ".env").write_text("PRESERVE_ME=yes\nPUBLIC_BASE_URL=" + OLD_URL + "\n")
    environment = {"TUNNEL_PROVIDER": "cloudflare", "PUBLIC_BASE_URL": OLD_URL}
    assert dev.ensure_tunnel(environment) == URL
    state = dev.read_state()
    assert state["app"] == app
    assert state["public_url"] == state["cloudflared"]["public_url"] == URL
    assert state["tunnel_provider"] == "cloudflare"
    assert sandbox.launched == [("cloudflared", ["/test/cloudflared", "tunnel", "--no-autoupdate", "--protocol", "auto",
        "--url", dev.BASE, "--metrics", "127.0.0.1:4041", "--output", "json"], environment)]
    assert (dev.ROOT / ".env").read_text() == "PRESERVE_ME=yes\nPUBLIC_BASE_URL=" + URL + "\n"
    assert stat.S_IMODE(dev.STATE.stat().st_mode) == 0o600
    assert stat.S_IMODE(dev.RUNTIME.stat().st_mode) == 0o700
    assert stat.S_IMODE((dev.ROOT / ".env").stat().st_mode) == 0o600


def test_live_cloudflare_recovers_url_without_new_connector(sandbox):
    record = write_log_record(dev.RUNTIME / "cloudflared.log", (OLD_URL + "\n").encode())
    dev.write_state({"cloudflared": record, "app": {"pid": 999}})
    assert dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"}) == URL
    assert sandbox.launched == []
    assert dev.read_state()["cloudflared"]["public_url"] == URL


def test_current_log_offset_does_not_reuse_old_url(sandbox):
    record = write_log_record(dev.RUNTIME / "cloudflared.log", (OLD_URL + "\n").encode(), b"starting\n")
    dev.write_state({"cloudflared": record, "public_url": OLD_URL})
    with pytest.raises(RuntimeError, match="no duplicate"):
        dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"})
    assert dev.read_state()["public_url"] == OLD_URL
    assert sandbox.launched == sandbox.stopped == []


@pytest.mark.parametrize("candidate", ["http://x.trycloudflare.com", "https://x.trycloudflare.com.evil.test",
    "https://x.trycloudflare.com/voice", "https://x.trycloudflare.com:443", "https://x.trycloudflare.com?x=1",
    "https://user@x.trycloudflare.com", "https://-x.trycloudflare.com", "https://x-.trycloudflare.com",
    "https://" + "x" * 64 + ".trycloudflare.com"])
def test_rejects_non_origin_quick_tunnel_urls(sandbox, candidate):
    record = write_log_record(dev.RUNTIME / "cloudflared.log", content=(candidate + "\n").encode())
    assert dev.cloudflare_url(record) is None


def test_json_log_format_parses_current_url(sandbox):
    record = write_log_record(dev.RUNTIME / "cloudflared.log",
        content=json.dumps({"level": "info", "message": "| " + URL + " |"}).encode())
    assert dev.cloudflare_url(record) == URL


@pytest.mark.parametrize("mutation", ["inode", "device", "truncate", "symlink", "fifo"])
def test_replaced_or_unsafe_logs_cannot_supply_url(sandbox, mutation):
    path = dev.RUNTIME / "cloudflared.log"
    record = write_log_record(path, b"old-data\n")
    if mutation == "inode":
        record["log_inode"] += 1
    elif mutation == "device":
        record["log_device"] += 1
    elif mutation == "truncate":
        path.write_bytes(b"")
    else:
        path.unlink()
        if mutation == "symlink":
            target = dev.ROOT / "not-a-log"
            target.write_text(URL)
            path.symlink_to(target)
        else:
            os.mkfifo(path)
    assert dev.cloudflare_url(record) is None


def test_cached_live_url_survives_log_rotation(sandbox):
    record = {"pid": 123, "identity": "owned", "public_url": URL}
    dev.write_state({"cloudflared": record})
    assert dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"}) == URL
    assert sandbox.launched == []


def test_dead_connector_gets_new_url_not_previous_state_url(sandbox):
    record = write_log_record(dev.RUNTIME / "cloudflared.log", content=(OLD_URL + "\n").encode())
    record.update(identity="dead", public_url=OLD_URL)
    dev.write_state({"cloudflared": record, "public_url": OLD_URL, "app": {"pid": 777}})
    assert dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"}) == URL
    assert len(sandbox.launched) == 1
    assert dev.read_state()["app"] == {"pid": 777}


def test_live_unhealthy_connector_is_preserved_without_duplicates(sandbox):
    dev.write_state({"cloudflared": {"pid": 123, "identity": "owned", "public_url": URL}})
    sandbox.ready = False
    with pytest.raises(RuntimeError, match="not ready"):
        dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"})
    assert sandbox.launched == sandbox.stopped == []
    sandbox.ready = True
    assert dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"}) == URL
    assert sandbox.launched == []


def test_untracked_metrics_listener_is_never_adopted_or_stopped(sandbox, monkeypatch):
    monkeypatch.setattr(dev, "available", lambda port: False)
    with pytest.raises(RuntimeError, match="untracked"):
        dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"})
    assert sandbox.launched == sandbox.stopped == []


def test_existing_cloudflare_config_refuses_launch_without_modifying_it(sandbox, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: dev.ROOT)
    config = dev.ROOT / ".cloudflared/config.yaml"
    config.parent.mkdir()
    config.write_text("tunnel: existing-user-tunnel\n")
    with pytest.raises(RuntimeError, match="existing cloudflared config"):
        dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"})
    assert config.read_text() == "tunnel: existing-user-tunnel\n"
    assert sandbox.launched == sandbox.stopped == []


def test_metrics_reply_from_other_pid_is_not_readiness(sandbox, monkeypatch):
    record = {"pid": 123, "identity": "owned", "public_url": URL}
    monkeypatch.setattr(dev, "listener_pids", lambda port: {987})
    assert dev.cloudflare_ready(record) is False


@pytest.mark.parametrize("data", [None, {}, {"status": 503, "readyConnections": 1},
    {"status": 200, "readyConnections": 0}, {"status": 200, "readyConnections": True},
    {"status": 200, "readyConnections": "1"}])
def test_readiness_requires_connected_status(sandbox, monkeypatch, data):
    record = {"pid": 123, "identity": "owned"}
    monkeypatch.setattr(dev, "listener_pids", lambda port: {123})
    monkeypatch.setattr(dev, "request", lambda url: data)
    assert dev.cloudflare_ready(record) is False


def test_ngrok_remains_default_and_reuses_unowned_matching_tunnel(sandbox):
    sandbox.ngrok_url = "https://existing.ngrok-free.app"
    assert dev.ensure_tunnel({}) == sandbox.ngrok_url
    assert sandbox.launched == sandbox.stopped == []
    assert "ngrok" not in dev.read_state()


def test_ngrok_dead_record_is_not_claimed_when_reusing_external_tunnel(sandbox):
    sandbox.ngrok_url = "https://existing.ngrok-free.app"
    dev.write_state({"ngrok": {"pid": 444, "identity": "dead"}})
    assert dev.ensure_tunnel({}) == sandbox.ngrok_url
    assert "ngrok" not in dev.read_state()
    assert sandbox.launched == []


def test_ngrok_launches_using_supplied_environment(sandbox):
    environment = {"TUNNEL_PROVIDER": "ngrok", "PATH": "/test"}
    assert dev.ensure_tunnel(environment) == "https://example.ngrok-free.app"
    assert sandbox.launched[0][0] == "ngrok"
    assert sandbox.launched[0][2] == environment


def test_provider_switch_stops_only_recorded_previous_provider(sandbox):
    ngrok = {"pid": 456, "identity": "owned"}
    app = {"pid": 789, "identity": "owned"}
    dev.write_state({"ngrok": ngrok, "app": app})
    assert dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"}) == URL
    assert sandbox.stopped == [(ngrok, 5)]
    assert dev.read_state()["app"] == app
    assert "ngrok" not in dev.read_state()


def test_failed_switch_keeps_previous_provider_and_url(sandbox):
    ngrok = {"pid": 456, "identity": "owned"}
    dev.write_state({"ngrok": ngrok, "public_url": OLD_URL})
    sandbox.ready = False
    with pytest.raises(RuntimeError, match="not ready"):
        dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"})
    assert sandbox.stopped == []
    assert dev.read_state()["ngrok"] == ngrok
    assert dev.read_state()["public_url"] == OLD_URL


def test_cancellation_is_checked_before_any_launch(sandbox):
    class Stopped(Exception):
        pass

    def stopping():
        raise Stopped()

    with pytest.raises(Stopped):
        dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"}, stopping=stopping)
    assert sandbox.launched == []


def test_cancellation_during_readiness_preserves_owned_connector(sandbox):
    sandbox.ready = False
    calls = [0]

    def stopping():
        calls[0] += 1
        return calls[0] == 4

    with pytest.raises(RuntimeError, match="stopped"):
        dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"}, stopping=stopping)
    assert len(sandbox.launched) == 1
    assert dev.read_state()["cloudflared"]["pid"] == 123
    assert sandbox.stopped == []


def test_duplicate_controller_lock_rejects_second_launcher(sandbox):
    with dev.tunnel_lock(), pytest.raises(RuntimeError, match="Another controller"):
        dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"})
    assert sandbox.launched == []


def test_provider_dotenv_precedence_and_validation(sandbox, monkeypatch):
    monkeypatch.setenv("TUNNEL_PROVIDER", "ngrok")
    (dev.ROOT / ".env").write_text("export TUNNEL_PROVIDER='cloudflare' # demo\n")
    assert dev.tunnel_provider(dev.tunnel_environment()) == "cloudflare"
    with pytest.raises(RuntimeError, match="must be"):
        dev.ensure_tunnel({"TUNNEL_PROVIDER": "unrecognized"})
    assert sandbox.launched == []


def test_status_supports_cloudflare(sandbox, monkeypatch, capsys):
    dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare"})
    state = dev.read_state()
    state["app"] = {"pid": 999, "identity": "owned"}
    dev.write_state(state)
    monkeypatch.setenv("TUNNEL_PROVIDER", "cloudflare")
    old_request = dev.request
    monkeypatch.setattr(dev, "request", lambda url: {} if url == dev.BASE + "/health" else old_request(url))
    assert dev.status() == 0
    assert "cloudflare: " + URL in capsys.readouterr().out


def test_spawn_records_log_generation_before_process_output(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "RUNTIME", tmp_path / ".runtime")
    monkeypatch.setattr(dev, "STATE", tmp_path / ".runtime/dev.json")
    dev.RUNTIME.mkdir()
    log = dev.RUNTIME / "cloudflared.log"
    log.write_text(OLD_URL + "\n")
    old_size = log.stat().st_size
    monkeypatch.setattr(dev, "identity", lambda pid: "process-identity")
    popen = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(dev.subprocess, "Popen", popen)
    state = {"app": {"pid": 999}}
    dev.spawn("cloudflared", ["cloudflared"], state, environment={"PATH": "/test"})
    assert state["cloudflared"]["log_offset"] == old_size
    assert state["cloudflared"]["log_inode"] == log.stat().st_ino
    assert state["app"] == {"pid": 999}
    assert popen.call_args.kwargs["env"] == {"PATH": "/test"}
    assert popen.call_args.kwargs["start_new_session"] is True
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert dev.cloudflare_url(state["cloudflared"]) is None


def test_terminate_never_signals_recycled_or_untracked_process(monkeypatch):
    monkeypatch.setattr(dev, "identity", lambda pid: "different-process")
    kill = Mock()
    monkeypatch.setattr(dev.os, "killpg", kill)
    dev.terminate({"pid": 123, "identity": "original-process"})
    kill.assert_not_called()


def test_listener_pid_parser(tmp_path, monkeypatch):
    monkeypatch.setattr(dev.shutil, "which", lambda executable: "/usr/sbin/lsof")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="p123\nf5\np456\nf9\n"))
    monkeypatch.setattr(dev.subprocess, "run", run)
    assert dev.listener_pids(4041) == {123, 456}
    assert run.call_args.kwargs["timeout"] == 3


def test_stop_handles_both_providers(sandbox):
    records = {"app": {"pid": 1}, "ngrok": {"pid": 2}, "cloudflared": {"pid": 3}}
    dev.write_state(records)
    assert dev.stop() == 0
    assert sandbox.stopped == [(records["app"], 40), (records["ngrok"], 5), (records["cloudflared"], 5)]
    assert not dev.STATE.exists()


def test_http2_override_from_dotenv_reaches_cloudflare(sandbox):
    (dev.ROOT / ".env").write_text("TUNNEL_PROVIDER=cloudflare\nTUNNEL_TRANSPORT_PROTOCOL=http2\n")
    dev.ensure_tunnel(dev.tunnel_environment())
    args = sandbox.launched[0][1]
    assert args[args.index("--protocol") + 1] == "http2"


def test_invalid_cloudflare_protocol_never_launches_connector(sandbox):
    with pytest.raises(RuntimeError, match="TUNNEL_TRANSPORT_PROTOCOL"):
        dev.ensure_tunnel({"TUNNEL_PROVIDER": "cloudflare", "TUNNEL_TRANSPORT_PROTOCOL": "invalid"})
    assert sandbox.launched == []
