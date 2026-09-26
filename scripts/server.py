"""Manage the macOS login service that runs this computer as the app server."""

import argparse
import fcntl
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.newcollege.passive-operator"
DOMAIN = f"gui/{os.getuid()}"
SERVICE = f"{DOMAIN}/{LABEL}"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
PYTHON = ROOT / ".venv/bin/python"
RUNTIME = ROOT / ".runtime"


def launch(*arguments, check=True):
    result = subprocess.run(["/bin/launchctl", *arguments], capture_output=True, text=True, timeout=20)
    if check and result.returncode:
        raise RuntimeError(f"launchctl {arguments[0]} failed: {result.stderr.strip()}")
    return result


def installed():
    return launch("print", SERVICE, check=False).returncode == 0


def verify_ownership():
    if PLIST.exists():
        with PLIST.open("rb") as handle:
            config = plistlib.load(handle)
        if (config.get("WorkingDirectory") != str(ROOT)
                or str(ROOT / "scripts/deploy.py") not in config.get("ProgramArguments", [])):
            raise RuntimeError("This service belongs to another checkout. Manage it from that checkout.")
    elif installed():
        raise RuntimeError("A service with this label is loaded without its expected plist; inspect it before changing it.")


def unload():
    launch("bootout", SERVICE)
    lock = RUNTIME / "deploy/supervisor.lock"
    if lock.exists():
        with lock.open("r+") as handle:
            deadline = time.monotonic() + 55
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Supervisor is still stopping; app and tunnel were left running.")
                    time.sleep(0.1)


def install():
    if sys.platform != "darwin":
        raise RuntimeError("This installer is for macOS. Run scripts/deploy.py run under your host's service manager.")
    if not PYTHON.exists():
        raise RuntimeError("Install the root .venv and requirements-lock.txt first.")
    verify_ownership()
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    RUNTIME.chmod(0o700)
    for filename in ("server.log", "server-error.log"):
        logfile = RUNTIME / filename
        logfile.touch(mode=0o600, exist_ok=True)
        logfile.chmod(0o600)
    config = {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", str(PYTHON), str(ROOT / "scripts/deploy.py"), "run"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 60,
        "StandardOutPath": str(RUNTIME / "server.log"),
        "StandardErrorPath": str(RUNTIME / "server-error.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                 "PYTHONUNBUFFERED": "1"},
    }
    if installed():
        unload()
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with PLIST.open("wb") as handle:
        plistlib.dump(config, handle)
    PLIST.chmod(0o600)
    start()
    print("Installed login service. Automatic deployment is enabled; idle sleep is inhibited while running.")


def start():
    verify_ownership()
    if not PLIST.exists():
        raise RuntimeError("Run python3 scripts/server.py install first.")
    launch("enable", SERVICE)
    if not installed():
        launch("bootstrap", DOMAIN, str(PLIST))
    print("Server service enabled.")


def stop():
    verify_ownership()
    launch("disable", SERVICE)
    if installed():
        unload()
    subprocess.run([str(PYTHON), str(ROOT / "scripts/dev.py"), "stop"], check=True, timeout=20)
    print("Server stopped: deployment watcher, managed app, and managed ngrok. Use start to resume.")


def status():
    result = launch("print", SERVICE, check=False)
    print("Login service: " + ("loaded" if result.returncode == 0 else "stopped"))
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.strip().startswith(("state =", "pid =", "last exit code =")):
                print(line.strip())
    subprocess.run([str(PYTHON), str(ROOT / "scripts/deploy.py"), "status"], check=False, timeout=10)
    subprocess.run([str(PYTHON), str(ROOT / "scripts/dev.py"), "status"], check=False, timeout=10)


def remove():
    stop()
    PLIST.unlink(missing_ok=True)
    print("Removed the login service. Source code and runtime data remain.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "start", "status", "stop", "remove"))
    args = parser.parse_args()
    try:
        globals()[args.command]()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
