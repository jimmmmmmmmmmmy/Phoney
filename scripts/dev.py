#!/usr/bin/env python3
"""Start, inspect, or stop the local Build 0 server and ngrok tunnel."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
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


def read_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def write_state(state):
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    os.chmod(RUNTIME, 0o700)
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle, indent=2)


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


def public_url(data):
    for tunnel in (data or {}).get("tunnels", []):
        target = tunnel.get("config", {}).get("addr", "")
        target = urlparse(target if "://" in target else "http://" + target)
        public = tunnel.get("public_url", "")
        if (target.hostname in ("127.0.0.1", "localhost") and target.port == 8000
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


def spawn(name, command, state):
    log = RUNTIME / (name + ".log")
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    environment = dict(os.environ)
    if name == "app":
        environment["PUBLIC_BASE_URL"] = state["public_url"]
    with os.fdopen(fd, "ab") as output:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True, env=environment)
    record = {"pid": process.pid, "identity": identity(process.pid)}
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


def status():
    state = read_state()
    url = public_url(request(TUNNELS))
    healthy = owned(state.get("app")) and request(BASE + "/health") is not None
    print("App: " + ("healthy at " + BASE if healthy else "not running or unhealthy"))
    print("ngrok: " + (url if url else "no HTTPS tunnel to port 8000"))
    if url:
        print("Voice webhook: " + url + "/voice")
    ngrok_alive = "ngrok" not in state or owned(state["ngrok"])
    return 0 if healthy and ngrok_alive and url and url == state.get("public_url") else 1


def start():
    previous = read_state()
    if any(owned(previous.get(name)) for name in ("app", "ngrok")):
        if status() == 0:
            return 0
        raise RuntimeError("Recorded services are incomplete. Run 'python3 scripts/dev.py stop', then start.")
    python = ROOT / ".venv/bin/python"
    if not python.exists():
        raise RuntimeError("Missing .venv/bin/python; install the project dependencies first.")
    if not available(8000):
        raise RuntimeError("Port 8000 is occupied by an untracked process; no processes were stopped.")
    data = request(TUNNELS)
    url = public_url(data)
    if not url and (data is not None or not available(4040)):
        raise RuntimeError("Port 4040 is in use without a matching ngrok tunnel; no processes were stopped.")
    if not url and not shutil.which("ngrok"):
        raise RuntimeError("ngrok is not installed or is not on PATH.")
    state = {}
    write_state(state)
    try:
        if not url:
            ngrok = spawn("ngrok", ["ngrok", "http", BASE, "--log", "stdout", "--log-format", "json"], state)
            deadline = time.monotonic() + 25
            while not url and ngrok.poll() is None and time.monotonic() < deadline:
                time.sleep(0.3)
                url = public_url(request(TUNNELS))
            if not url:
                raise RuntimeError("ngrok did not start; inspect .runtime/ngrok.log.")
        persist_url(url)
        state["public_url"] = url
        app = spawn("app", [str(python), "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                            "--port", "8000", "--no-access-log"], state)
        deadline = time.monotonic() + 20
        while app.poll() is None and time.monotonic() < deadline:
            if request(BASE + "/health") is not None:
                if status() == 0:
                    return 0
                raise RuntimeError("App started but the ngrok tunnel changed; start again.")
            time.sleep(0.3)
        raise RuntimeError("App did not become healthy; inspect .runtime/app.log.")
    except BaseException:
        for name in ("app", "ngrok"):
            terminate(state.get(name), timeout=40 if name == "app" else 5)
        STATE.unlink(missing_ok=True)
        raise


def stop():
    state = read_state()
    for name in ("app", "ngrok"):
        terminate(state.get(name), timeout=40 if name == "app" else 5)
    STATE.unlink(missing_ok=True)
    print("Stopped recorded app and ngrok processes. Reused tunnels remain running.")
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
