#!/usr/bin/env python3
"""Start, inspect, or stop the local server and its ngrok/Cloudflare tunnel."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
STATE = RUNTIME / "dev.json"
BASE = "http://127.0.0.1:8000"
TUNNELS = "http://127.0.0.1:4040/api/tunnels"
CLOUDFLARE_READY = "http://127.0.0.1:4041/ready"
QUICK_URL = re.compile(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com")


def request(url):
    try:
        with urlopen(url, timeout=2) as response:
            return json.load(response)
    except (OSError, ValueError, URLError):
        return None


def identity(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "args="],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def owned(record):
    return bool(record and record.get("identity") and identity(record["pid"]) == record["identity"])


def refresh_child_identity(process, record):
    """Follow launcher exec only while Popen proves this is our unreaped child.

    macOS framework Python can replace its argv[0] after Popen returns. A
    matching live child cannot have its PID recycled, unlike a recovered PID.
    Recovered state must continue to pass the full, unchanged owned() check.
    """
    if process.pid != record.get("pid") or process.poll() is not None:
        return False
    current = identity(process.pid)
    if not current:
        return False
    record["identity"] = current
    return True


def read_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def write_state(state):
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    os.chmod(RUNTIME, 0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=RUNTIME, prefix=".dev.", delete=False) as handle:
        temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        json.dump(state, handle, indent=2)
    os.replace(temporary, STATE)


def available(port):
    try:
        with socket.socket() as sock:
            # Match uvicorn's reuse policy: a closed HTTP connection in TIME_WAIT
            # must not prevent a replacement server from taking this port.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def app_port(environment=None):
    environment = tunnel_environment() if environment is None else environment
    value = environment.get("APP_PORT", "8000")
    if (not isinstance(value, str) or not re.fullmatch(r"[0-9]{4,5}", value)
            or not 1024 <= int(value) <= 65534):
        raise RuntimeError("APP_PORT must be an integer from 1024 through 65534.")
    port = int(value)
    if port in (4039, 4040, 4041):
        raise RuntimeError("APP_PORT and its candidate port must not overlap tunnel ports 4040 or 4041.")
    return port


def base_url(environment=None):
    return "http://127.0.0.1:" + str(app_port(environment))


def public_url(data, environment=None):
    port = app_port(environment)
    for tunnel in (data or {}).get("tunnels", []):
        target = tunnel.get("config", {}).get("addr", "")
        target = urlparse(target if "://" in target else "http://" + target)
        public = tunnel.get("public_url", "")
        if (target.hostname in ("127.0.0.1", "localhost") and target.port == port
                and public.startswith("https://")):
            return public.rstrip("/")
    return None


def persist_url(url):
    path = ROOT / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if not re.match(r"^\s*(?:export\s+)?PUBLIC_BASE_URL\s*=", line)]
    lines.append("PUBLIC_BASE_URL=" + url)
    with tempfile.NamedTemporaryFile(mode="w", dir=ROOT, prefix=".env.", delete=False) as handle:
        temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        handle.write("\n".join(lines) + "\n")
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def spawn(name, command, state, environment=None):
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    log = RUNTIME / (name + ".log")
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    os.fchmod(fd, 0o600)
    log_stat = os.fstat(fd)
    if not stat.S_ISREG(log_stat.st_mode):
        os.close(fd)
        raise RuntimeError("Refusing a non-file process log.")
    environment = dict(os.environ if environment is None else environment)
    if name == "app":
        environment["PUBLIC_BASE_URL"] = state["public_url"]
    with os.fdopen(fd, "ab") as output:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True, env=environment)
    record = {"pid": process.pid, "identity": identity(process.pid)}
    if name == "app":
        record["port"] = app_port()
    if name == "cloudflared":
        record.update(log_offset=log_stat.st_size, log_inode=log_stat.st_ino,
                      log_device=log_stat.st_dev)
    state[name] = record
    write_state(state)
    return process


def terminate(record, timeout=5):
    if not owned(record):
        return
    pid = record["pid"]
    for sig, delay in ((signal.SIGTERM, timeout), (signal.SIGKILL, 1)):
        if not owned(record):
            return
        try:
            if os.getpgid(pid) != pid:
                raise RuntimeError("Refusing to stop a process without its own process group.")
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and owned(record):
            time.sleep(0.1)


def terminate_child(process, record, timeout=5):
    """Stop our live Popen child even if a launcher changes argv during cleanup."""
    if process.pid != record.get("pid"):
        raise RuntimeError("Refusing to stop a mismatched child process.")
    for sig, delay in ((signal.SIGTERM, timeout), (signal.SIGKILL, 1)):
        if process.poll() is not None:
            return
        try:
            if os.getpgid(process.pid) != process.pid:
                raise RuntimeError("Refusing to stop a child without its own process group.")
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=delay)
            return
        except subprocess.TimeoutExpired:
            if sig == signal.SIGKILL:
                raise


def tunnel_environment():
    """Keep the standalone CLI dependency-free; .env overrides tunnel settings."""
    environment = dict(os.environ)
    path = ROOT / ".env"
    for line in path.read_text().splitlines() if path.exists() else []:
        match = re.match(r"^\s*(?:export\s+)?(APP_PORT|TUNNEL_PROVIDER|TUNNEL_TRANSPORT_PROTOCOL|CLOUDFLARE_TUNNEL_CONFIG|CLOUDFLARE_PUBLIC_URL)\s*=\s*(.*?)\s*$", line)
        if match:
            value = match.group(2).split(" #", 1)[0].strip().strip("\"'")
            environment[match.group(1)] = value
    return environment


def tunnel_provider(environment):
    provider = environment.get("TUNNEL_PROVIDER", "ngrok").strip().lower()
    if provider not in ("ngrok", "cloudflare"):
        raise RuntimeError("TUNNEL_PROVIDER must be ngrok or cloudflare.")
    return provider


def cloudflare_configuration(environment):
    """Validate connector inputs without putting tunnel credentials in state or argv."""
    port = app_port(environment)
    protocol = environment.get("TUNNEL_TRANSPORT_PROTOCOL", "auto").strip().lower()
    if protocol not in ("auto", "http2", "quic"):
        raise RuntimeError("TUNNEL_TRANSPORT_PROTOCOL must be auto, http2, or quic.")
    config = environment.get("CLOUDFLARE_TUNNEL_CONFIG", "").strip()
    public = environment.get("CLOUDFLARE_PUBLIC_URL", "").strip()
    if not config and not public:
        configuration = {"mode": "quick", "protocol": protocol}
        if port != 8000:
            configuration["origin"] = base_url(environment)
        return configuration
    if not config or not public:
        raise RuntimeError("CLOUDFLARE_TUNNEL_CONFIG and CLOUDFLARE_PUBLIC_URL must be set together.")
    try:
        parsed = urlparse(public)
        hostname = parsed.hostname or ""
        valid_host = (len(hostname) <= 253 and all(re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in hostname.split(".")))
        valid = (parsed.scheme == "https" and valid_host and parsed.netloc == hostname
                 and not parsed.path and not parsed.params and not parsed.query and not parsed.fragment
                 and public == "https://" + hostname)
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("CLOUDFLARE_PUBLIC_URL must be an HTTPS hostname origin without credentials, port, path, query, or fragment.")
    path = Path(config)
    if not path.is_absolute():
        raise RuntimeError("CLOUDFLARE_TUNNEL_CONFIG must be an absolute path to a readable configuration file.")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError("not a regular file")
            content = handle.read(1024 * 1024 + 1)
            if not content or len(content) > 1024 * 1024:
                raise OSError("empty or oversized configuration")
    except OSError:
        raise RuntimeError("CLOUDFLARE_TUNNEL_CONFIG must be an existing readable regular file, not a symlink.") from None
    return {"mode": "named", "protocol": protocol, "config": str(path),
            "config_sha256": hashlib.sha256(content).hexdigest(), "public_url": public}


def cloudflare_matches(record, configuration):
    if not record:
        return False
    saved = record.get("configuration")
    # Older versions recorded only Quick Tunnels; preserve those healthy processes.
    return saved == configuration or (saved is None and configuration["mode"] == "quick"
                                     and "origin" not in configuration)


def tunnel_change_requires_drain(environment, state):
    provider = tunnel_provider(environment)
    configuration = cloudflare_configuration(environment) if provider == "cloudflare" else None
    if provider != state.get("tunnel_provider", "ngrok"):
        return True
    record = state.get("cloudflared")
    return bool(configuration and record and not cloudflare_matches(record, configuration))


@contextmanager
def tunnel_lock():
    """Prevent two CLI/supervisor processes from launching duplicate connectors."""
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    os.chmod(RUNTIME, 0o700)
    fd = os.open(RUNTIME / "tunnel.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another controller is preparing the tunnel; retry shortly.") from error
        yield
    finally:
        os.close(fd)


def listener_pids(port):
    """An HTTP response alone cannot establish ownership of the metrics port."""
    executable = shutil.which("lsof")
    if not executable:
        raise RuntimeError("lsof is required to verify Cloudflare metrics port ownership.")
    try:
        result = subprocess.run([executable, "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-Fp"],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Could not verify Cloudflare metrics port ownership.") from error
    if result.returncode not in (0, 1):
        raise RuntimeError("Could not verify Cloudflare metrics port ownership.")
    return {int(line[1:]) for line in result.stdout.splitlines()
            if re.fullmatch(r"p[0-9]+", line)}


def cloudflare_url(record):
    """Read only this process's log, never a previous connector's random URL."""
    if not record:
        return None
    cached = record.get("public_url")
    if isinstance(cached, str) and QUICK_URL.fullmatch(cached):
        return cached
    offset = record.get("log_offset")
    if type(offset) is not int or offset < 0:
        return None
    try:
        fd = os.open(RUNTIME / "cloudflared.log", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size < offset
                    or info.st_ino != record.get("log_inode")
                    or info.st_dev != record.get("log_device")):
                return None
            handle.seek(offset)
            content = handle.read(1024 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return None
    for candidate in re.findall(r'https://[^\s"\\|]+', content):
        if QUICK_URL.fullmatch(candidate):
            return candidate
    return None


def cloudflare_ready(record):
    if not owned(record):
        return False
    if listener_pids(4041) != {record["pid"]}:
        return False
    data = request(CLOUDFLARE_READY)
    return bool(isinstance(data, dict) and data.get("status") == 200
                and type(data.get("readyConnections")) is int and data["readyConnections"] > 0)


def check_stopping(stopping):
    if stopping and stopping():
        raise RuntimeError("Tunnel startup was stopped.")


def _ensure_ngrok(state, environment, stopping):
    record = state.get("ngrok")
    data = request(TUNNELS)
    url = public_url(data, environment)
    if url:
        if record and not owned(record):
            state.pop("ngrok", None)  # Reuse the unowned tunnel without claiming its PID.
        return url
    if not owned(record):
        if data is not None or not available(4040):
            raise RuntimeError("Port 4040 is in use without a matching ngrok tunnel; no processes were stopped.")
        executable = shutil.which("ngrok")
        if not executable:
            raise RuntimeError("ngrok is not installed or is not on PATH.")
        check_stopping(stopping)
        spawn("ngrok", [executable, "http", base_url(environment), "--log", "stdout", "--log-format", "json"],
              state, environment=environment)
    deadline = time.monotonic() + 25
    while owned(state.get("ngrok")) and time.monotonic() < deadline:
        check_stopping(stopping)
        url = public_url(request(TUNNELS), environment)
        if url:
            return url
        time.sleep(0.3)
    raise RuntimeError("ngrok is not ready; inspect .runtime/ngrok.log. Its recorded process was preserved.")


def _ensure_cloudflare(state, environment, stopping):
    configuration = cloudflare_configuration(environment)
    protocol = configuration["protocol"]
    named = configuration["mode"] == "named"
    record = state.get("cloudflared")
    if not owned(record) or not cloudflare_matches(record, configuration):
        if not available(4041) and (not owned(record) or listener_pids(4041) != {record["pid"]}):
            raise RuntimeError("Port 4041 belongs to an untracked process; no processes were stopped.")
        executable = shutil.which("cloudflared")
        if not executable:
            raise RuntimeError("cloudflared is not installed or is not on PATH.")
        config_dirs = (Path.home() / ".cloudflared", Path.home() / ".cloudflare-warp",
                       Path.home() / "cloudflare-warp", Path("/etc/cloudflared"),
                       Path("/usr/local/etc/cloudflared"), Path("/opt/homebrew/etc/cloudflared"))
        if not named and any((directory / name).exists() for directory in config_dirs
                             for name in ("config.yml", "config.yaml")):
            raise RuntimeError("An existing cloudflared config may override Quick Tunnel settings. "
                               "Move it aside before starting this demo; it was not modified.")
        # Verify that ownership checks can run before launching a connector.
        listener_pids(4041)
        check_stopping(stopping)
        if named:
            try:
                validated = subprocess.run([executable, "tunnel", "--config", configuration["config"],
                                            "ingress", "validate"], capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                raise RuntimeError("Cloudflare named tunnel configuration could not be validated; the existing connector was preserved.") from None
            if validated.returncode:
                raise RuntimeError("Cloudflare named tunnel configuration failed ingress validation; the existing connector was preserved.")
        check_stopping(stopping)
        if owned(record):
            terminate(record, timeout=5)
            if not available(4041):
                raise RuntimeError("Previous Cloudflare connector is still using port 4041; no replacement was started.")
            state.pop("cloudflared", None)
            write_state(state)
        command = [executable, "tunnel", "--no-autoupdate", "--protocol", protocol]
        command += ["--config", configuration["config"]] if named else ["--url", base_url(environment)]
        command += ["--metrics", "127.0.0.1:4041", "--output", "json"]
        if named:
            command.append("run")
        spawn("cloudflared", command, state, environment=environment)
        record = state["cloudflared"]
        record["configuration"] = configuration
        write_state(state)
    deadline = time.monotonic() + 40
    while owned(record) and time.monotonic() < deadline:
        check_stopping(stopping)
        url = configuration["public_url"] if named else cloudflare_url(record)
        if url and cloudflare_ready(record):
            record["public_url"] = url
            return url
        time.sleep(0.3)
    raise RuntimeError("Cloudflare is not ready; inspect .runtime/cloudflared.log. "
                       "Its recorded process was preserved for recovery; no duplicate was started.")


def ensure_tunnel(environment, stopping=None):
    """Return and persist the selected provider's ready HTTPS origin.

    Existing owned connectors survive app deploys and supervisor restarts. The
    callback may raise the supervisor's stop exception, or return True to stop.
    """
    app_port(environment)  # Reject invalid configuration before writing state or stopping processes.
    provider = tunnel_provider(environment)
    check_stopping(stopping)
    with tunnel_lock():
        state = read_state()
        url = (_ensure_cloudflare if provider == "cloudflare" else _ensure_ngrok)(
            state, environment, stopping)
        check_stopping(stopping)
        old = "ngrok" if provider == "cloudflare" else "cloudflared"
        if old in state:
            terminate(state[old], timeout=5)  # terminate verifies saved process identity.
            state.pop(old)
        if environment.get("PUBLIC_BASE_URL") != url:
            persist_url(url)
        state.update(public_url=url, tunnel_provider=provider)
        write_state(state)
        return url


def status():
    state = read_state()
    environment = tunnel_environment()
    port, base = app_port(environment), base_url(environment)
    provider = tunnel_provider(environment)
    if provider == "cloudflare":
        configuration = cloudflare_configuration(environment)
        record = state.get("cloudflared")
        ready = cloudflare_matches(record, configuration) and cloudflare_ready(record)
        url = (configuration["public_url"] if configuration["mode"] == "named"
               else cloudflare_url(record)) if ready else None
        tunnel_alive = bool(url)
    else:
        url = public_url(request(TUNNELS), environment)
        tunnel_alive = "ngrok" not in state or owned(state["ngrok"])
    healthy = owned(state.get("app")) and request(base + "/health") is not None
    print("App: " + ("healthy at " + base if healthy else "not running or unhealthy"))
    print(provider + ": " + (url if url else "no ready HTTPS tunnel to port " + str(port)))
    if url:
        print("Voice webhook: " + url + "/voice")
    return 0 if healthy and tunnel_alive and url and url == state.get("public_url") else 1


def start():
    environment = tunnel_environment()
    port, base = app_port(environment), base_url(environment)
    previous = read_state()
    if owned(previous.get("app")):
        if status() == 0:
            return 0
        raise RuntimeError("Recorded services are incomplete. Run 'python3 scripts/dev.py stop', then start.")
    python = ROOT / ".venv/bin/python"
    if not python.exists():
        raise RuntimeError("Missing .venv/bin/python; install the project dependencies first.")
    if not available(port):
        raise RuntimeError(f"Port {port} is occupied by an untracked process; no processes were stopped.")
    url = ensure_tunnel(environment)
    state = read_state()
    app = None
    try:
        app = spawn("app", [str(python), "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                            "--port", str(port), "--no-access-log",
                            "--ws-max-size", "65536", "--ws-max-queue", "16"], state)
        deadline = time.monotonic() + 20
        while app.poll() is None and time.monotonic() < deadline:
            if request(base + "/health") is not None:
                refresh_child_identity(app, state["app"])
                write_state(state)
                if status() == 0:
                    return 0
                raise RuntimeError("App started but the tunnel changed; start again.")
            time.sleep(0.3)
        raise RuntimeError("App did not become healthy; inspect .runtime/app.log.")
    except BaseException:
        if app is not None:
            try:
                refresh_child_identity(app, state["app"])
            finally:
                terminate_child(app, state["app"], timeout=40)
        state.pop("app", None)
        write_state(state)  # Keep the ready connector and URL for a later start.
        raise


def stop():
    state = read_state()
    for name in ("app", "ngrok", "cloudflared"):
        if name in state:
            terminate(state[name], timeout=40 if name == "app" else 5)
    STATE.unlink(missing_ok=True)
    print("Stopped recorded app and tunnel processes. Unowned reused tunnels remain running.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "status", "stop"))
    args = parser.parse_args()
    try:
        sys.exit({"start": start, "status": status, "stop": stop}[args.command]())
    except (OSError, ValueError, RuntimeError) as error:
        print("Error: " + str(error), file=sys.stderr)
        sys.exit(1)
