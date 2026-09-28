#!/usr/bin/env python3
"""Supervise deployments of this repository's main branch without editing the checkout."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

from dotenv import dotenv_values

try:
    from . import dev
except ImportError:
    import dev

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "jimmmmmmmmmmmy/fictional-rotary-phone"
REMOTE = "https://github.com/" + REPOSITORY + ".git"
_UNREAD_HEALTH = object()


class DeploymentStopped(BaseException):
    """Stop an in-progress build without treating shutdown as a bad commit."""


class DeploymentDeferred(Exception):
    """Keep the current process while calls or their cleanup prevent replacement."""


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def validate_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Expected a full lowercase Git commit SHA.")
    return value


def atomic_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        json.dump(data, handle, indent=2)
    os.replace(temporary, path)


@contextmanager
def exclusive_lock(path):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A deployment supervisor is already running.") from None
        yield
    finally:
        os.close(descriptor)


class Supervisor:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.runtime = self.root / ".runtime"
        self.directory = self.runtime / "deploy"
        self.releases = self.directory / "releases"
        self.mirror = self.directory / "repo.git"
        self.state_path = self.directory / "state.json"
        self.trigger = self.runtime / "deploy.trigger"
        self.retry_path = self.directory / "retry"
        for path in (self.runtime, self.directory, self.releases):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.stopping = False

    def save(self, **changes):
        self.state.update(changes)
        atomic_json(self.state_path, self.state)

    def environment(self, sha=None):
        environment = dict(os.environ)
        environment.update({key: value for key, value in dotenv_values(self.root / ".env").items()
                            if value is not None})
        repository = environment.get("DEPLOY_REPOSITORY", REPOSITORY)
        if repository not in {REPOSITORY, REMOTE, REMOTE.removesuffix(".git")}:
            raise RuntimeError("DEPLOY_REPOSITORY must identify " + REPOSITORY)
        environment["DEPLOY_REPOSITORY"] = REPOSITORY
        environment["DEPLOY_TRIGGER_PATH"] = str(self.trigger)
        if sha:
            environment["DEPLOY_COMMIT"] = validate_sha(sha)
        dev.app_port(environment)
        return environment

    def app_port(self):
        return dev.app_port(self.environment())

    def candidate_port(self):
        return self.app_port() + 1

    def build_environment(self):
        # Test/install subprocesses do not inherit the application's production secrets.
        return {key: value for key, value in os.environ.items()
                if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE"}}

    def command(self, arguments, cwd=None, timeout=180):
        log = self.directory / "build.log"
        descriptor = os.open(log, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "ab") as output:
            process = subprocess.Popen([str(item) for item in arguments], cwd=cwd or self.root,
                                       env=self.build_environment(), stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if self.stopping:
                        raise DeploymentStopped()
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Deployment command timed out; inspect .runtime/deploy/build.log.")
                    try:
                        process.wait(timeout=min(1, max(0.01, deadline - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
                result = process.returncode
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
            if result:
                raise RuntimeError("Deployment command failed; inspect .runtime/deploy/build.log.")

    def fetch(self):
        self.environment()  # Validate the configured repository before reaching the network.
        if not self.mirror.exists():
            self.command(["git", "init", "--bare", self.mirror])
        self.command(["git", "--git-dir", self.mirror, "fetch", "--no-tags", REMOTE,
                      "+refs/heads/main:refs/heads/main"], timeout=90)
        result = subprocess.run(["git", "--git-dir", str(self.mirror), "rev-parse", "refs/heads/main"],
                                check=True, capture_output=True, text=True, timeout=10,
                                env=self.build_environment())
        return validate_sha(result.stdout.strip())

    def ensure_tunnel(self):
        def check_stopping():
            if self.stopping:
                raise DeploymentStopped()

        environment = self.environment()
        state = dev.read_state()
        self.validate_record_port(state.get("app"), dev.app_port(environment))
        if dev.tunnel_change_requires_drain(environment, state):
            # Keep the old connector carrying media until active calls finish.
            with self.drain(state.get("app")):
                return dev.ensure_tunnel(environment, stopping=check_stopping)
        return dev.ensure_tunnel(environment, stopping=check_stopping)

    def configure_hooks(self, url):
        if self.state.get("configured_public_url") == url:
            return
        python = self.root / ".venv/bin/python"
        for script in ("configure_twilio.py", "configure_github.py"):
            self.command([python, self.root / "scripts" / script, "--apply"], timeout=90)
        self.save(configured_public_url=url)

    def prepare(self, sha):
        release = self.releases / validate_sha(sha)
        if not release.exists():
            self.command(["git", "clone", "--shared", "--no-checkout", self.mirror, release])
            self.command(["git", "checkout", "--detach", sha], cwd=release)
        if release.is_symlink():
            raise RuntimeError("Release directory must not be a symlink.")
        actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=release, check=True,
                                capture_output=True, text=True, timeout=10).stdout.strip()
        if actual != sha:
            raise RuntimeError("Release checkout does not match its commit.")
        python = release / ".venv/bin/python"
        if not python.exists():
            self.command([sys.executable, "-m", "venv", release / ".venv"])
        requirements = release / "requirements-lock.txt"
        if not requirements.exists():
            requirements = release / "requirements-dev.txt"
        self.command([python, "-m", "pip", "install", "--disable-pip-version-check", "-r", requirements],
                     cwd=release, timeout=600)
        self.command([python, "-m", "pytest", "-q", "tests"], cwd=release, timeout=180)
        self.probe(release, sha)
        return release

    def spawn(self, release, sha, port):
        log = self.runtime / ("app.log" if port == self.app_port() else "deploy/candidate.log")
        descriptor = os.open(log, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "ab") as output:
            process = subprocess.Popen([str(release / ".venv/bin/python"), "-m", "uvicorn", "main:app",
                                        "--host", "127.0.0.1", "--port", str(port), "--no-access-log",
                                        "--ws-max-size", "65536", "--ws-max-queue", "16"],
                                       cwd=release, env=self.environment(sha), stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        record = {"pid": process.pid, "identity": dev.identity(process.pid), "release": str(release),
                  "commit": sha, "public_url": self.environment().get("PUBLIC_BASE_URL"), "port": port}
        return process, record

    def health(self, port=None):
        port = self.app_port() if port is None else port
        return dev.request("http://127.0.0.1:" + str(port) + "/health")

    @staticmethod
    def matches_health(result, sha):
        return isinstance(result, dict) and result.get("status") == "ok" and result.get("commit") == sha

    def healthy(self, sha, port=None):
        return self.matches_health(self.health(port), sha)

    def deployment_control(self, draining=None):
        token = self.environment().get("DEPLOY_CONTROL_TOKEN", "").strip()
        if not token:
            raise DeploymentDeferred("DEPLOY_CONTROL_TOKEN is required before replacing the active switchboard.")
        request = Request(dev.base_url(self.environment()) + "/internal/deploy", headers={"Authorization": "Bearer " + token})
        if draining is not None:
            request.method = "POST"
            request.add_header("Content-Type", "application/json")
            request.data = json.dumps({"draining": draining}).encode("utf-8")
        try:
            with urlopen(request, timeout=2) as response:
                state = json.load(response)
        except (OSError, ValueError):
            raise DeploymentDeferred("The active switchboard did not confirm its deployment state; preserving it.") from None
        if (not isinstance(state, dict) or type(state.get("draining")) is not bool
                or any(type(state.get(key)) is not int or state[key] < 0
                       for key in ("active_sessions", "pending_work"))
                or (draining is not None and state["draining"] != draining)):
            raise DeploymentDeferred("The active switchboard returned an invalid deployment state; preserving it.")
        return state

    @contextmanager
    def drain(self, record, health=_UNREAD_HEALTH):
        if not dev.owned(record):
            yield
            return
        health = self.health() if health is _UNREAD_HEALTH else health
        if (not isinstance(health, dict) or type(health.get("build")) is not int
                or health["build"] < 1 or health.get("switchboard_ready") is not True):
            yield
            return
        deadline = time.monotonic() + 60
        try:
            self.save(status="draining")
            state = self.deployment_control(True)
            while state["active_sessions"] or state["pending_work"]:
                if self.stopping:
                    raise DeploymentStopped()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeploymentDeferred("Active calls or pending call cleanup delayed deployment; retrying automatically.")
                time.sleep(min(1, remaining))
                state = self.deployment_control()
            if self.stopping:
                raise DeploymentStopped()
            yield
        finally:
            # A timeout, interruption, or failed termination must reopen the old
            # process to incoming calls. Newly launched processes start undrained.
            if dev.owned(record):
                try:
                    self.deployment_control(False)
                except DeploymentDeferred:
                    self.save(drain_reset_required=True)
                else:
                    self.save(drain_reset_required=False)

    def wait_healthy(self, process, sha, port):
        deadline = time.monotonic() + 20
        while process.poll() is None and time.monotonic() < deadline:
            if port == self.candidate_port() and self.stopping:
                raise DeploymentStopped()
            if self.healthy(sha, port):
                return
            time.sleep(0.25)
        raise RuntimeError("Release did not become healthy; inspect .runtime/app.log or deploy/candidate.log.")

    def probe(self, release, sha):
        port = self.candidate_port()
        if not dev.available(port):
            raise RuntimeError(f"Candidate port {port} is in use; the active app was left running.")
        process, record = self.spawn(release, sha, port)
        self.save(candidate_process=record)
        try:
            self.wait_healthy(process, sha, port)
        finally:
            try:
                dev.refresh_child_identity(process, record)
                self.save(candidate_process=record)
            finally:
                dev.terminate_child(process, record, timeout=40)
            process.wait(timeout=2)
            self.save(candidate_process=None)

    def launch(self, release, sha, state):
        port = self.app_port()
        if not dev.available(port):
            raise RuntimeError(f"Port {port} is occupied; no untracked process was stopped.")
        process, record = self.spawn(release, sha, port)
        state["app"] = record
        dev.write_state(state)  # Record ownership before waiting so a crash remains recoverable.
        try:
            self.wait_healthy(process, sha, port)
            dev.refresh_child_identity(process, record)
            dev.write_state(state)
        except BaseException:
            try:
                dev.refresh_child_identity(process, record)
                dev.write_state(state)
            finally:
                dev.terminate_child(process, record, timeout=40)
            process.wait(timeout=2)
            raise

    def activate(self, release, sha):
        port = self.app_port()
        state = dev.read_state()
        previous = self.state.get("active_commit")
        previous_record = state.get("app")
        self.validate_record_port(previous_record, port)
        if not dev.owned(previous_record) and not dev.available(port):
            raise RuntimeError(f"Port {port} belongs to an untracked process; deployment was not activated.")
        with self.drain(previous_record):
            dev.terminate(previous_record, timeout=40)
        try:
            self.launch(release, sha, state)
        except Exception:
            if previous:
                self.launch(self.releases / validate_sha(previous), previous, state)
            elif previous_record and not previous_record.get("commit"):
                # First deployment may be adopting the original dev.py checkout.
                python = self.root / ".venv/bin/python"
                if python.exists():
                    process = dev.spawn("app", [str(python), "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                                      "--port", str(port), "--no-access-log",
                                      "--ws-max-size", "65536", "--ws-max-queue", "16"], state)
                    try:
                        deadline = time.monotonic() + 20
                        while process.poll() is None and time.monotonic() < deadline:
                            if self.health() is not None:
                                dev.refresh_child_identity(process, state["app"])
                                dev.write_state(state)
                                break
                            time.sleep(0.25)
                        else:
                            raise RuntimeError("The previous app did not recover health after rollback.")
                    except BaseException:
                        dev.terminate_child(process, state["app"], timeout=40)
                        raise
            raise
        self.save(active_commit=sha, active_release=str(release), status="running", failed_commit=None,
                  deployed_at=timestamp(), last_error=None, candidate_commit=None)

    @staticmethod
    def validate_record_port(record, port):
        if dev.owned(record) and record.get("port", 8000) != port:
            raise DeploymentDeferred("APP_PORT changed while the previous app is running. "
                                     "Stop and restart the service after calls finish to change ports.")

    def recover(self):
        port = self.app_port()
        sha = self.state.get("active_commit")
        if not sha:
            return
        state = dev.read_state()
        record = state.get("app")
        self.validate_record_port(record, port)
        health = self.health() if dev.owned(record) else None
        if dev.owned(record) and self.state.get("drain_reset_required"):
            self.deployment_control(False)
            self.save(drain_reset_required=False)
        if (dev.owned(record) and self.matches_health(health, sha)
                and record.get("public_url") == self.environment().get("PUBLIC_BASE_URL")):
            return
        with self.drain(record, health):
            dev.terminate(record, timeout=40)
        self.launch(self.releases / validate_sha(sha), sha, state)
        self.save(status="running", recovered_at=timestamp())

    def check(self):
        self.save(checked_at=timestamp())
        if self.retry_path.exists():
            self.retry_path.unlink()
            self.save(failed_commit=None)
        sha = None
        try:
            # A saved release can serve locally even while the connector or
            # internet is unavailable. Do not make crash recovery wait for it.
            previous_url = self.environment().get("PUBLIC_BASE_URL")
            self.recover()
            url = self.ensure_tunnel()
            if self.environment().get("PUBLIC_BASE_URL") != previous_url:
                # Quick Tunnels can publish a new origin. Reconcile the app's
                # callback URLs after readiness, preserving the usual drain.
                self.recover()
            if self.state.get("active_commit"):
                self.configure_hooks(url)
            sha = self.fetch()
            self.save(remote_commit=sha)
            if sha == self.state.get("active_commit"):
                self.save(status="running", last_error=None)
                return
            if sha == self.state.get("failed_commit"):
                return
            self.save(status="preparing", candidate_commit=sha)
            release = self.prepare(sha)
            if self.stopping:
                raise DeploymentStopped()
            self.activate(release, sha)
            self.configure_hooks(url)
            print("Deployed " + sha, flush=True)
        except DeploymentDeferred as error:
            self.save(status="waiting", last_error=str(error), waiting_at=timestamp())
            print("Deployment waiting: " + str(error), flush=True)
        except Exception as error:
            changes = {"status": "error", "last_error": str(error), "failed_at": timestamp()}
            if sha:
                changes["failed_commit"] = sha
            self.save(**changes)
            print("Deployment error: " + str(error), flush=True)

    def run(self):
        with exclusive_lock(self.directory / "supervisor.lock"):
            def stop(signum, frame):
                self.stopping = True
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            self.save(supervisor_pid=os.getpid())
            dev.terminate(self.state.get("candidate_process"), timeout=40)
            self.save(candidate_process=None)
            next_check, last_signal = 0, None
            try:
                while not self.stopping:
                    marker = self.trigger.stat().st_mtime_ns if self.trigger.exists() else None
                    if time.monotonic() >= next_check or marker != last_signal or self.retry_path.exists():
                        # A signal arriving during this check remains visible next iteration.
                        last_signal = marker
                        self.check()
                        next_check = time.monotonic() + 30
                    time.sleep(1)
            except DeploymentStopped:
                pass
            finally:
                dev.terminate(self.state.get("candidate_process"), timeout=40)
                self.save(supervisor_pid=None, candidate_process=None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status", "retry"))
    args = parser.parse_args()
    supervisor = Supervisor()
    if args.command == "run":
        supervisor.run()
    elif args.command == "retry":
        supervisor.retry_path.touch(mode=0o600)
        print("Retry requested; the running supervisor will check within one second.")
    else:
        print(json.dumps(supervisor.state, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print("Error: " + str(error), file=sys.stderr)
        sys.exit(1)
