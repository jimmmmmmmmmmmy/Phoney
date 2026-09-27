"""Configure the repository's signed push webhook without printing secrets."""

import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings

REPOSITORY = "jimmmmmmmmmmmy/fictional-rotary-phone"
STATE = ROOT / ".runtime/github-hook.json"


def api(path, method="GET", data=None):
    command = ["gh", "api", path, "--method", method]
    if data is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        # Do not echo a submitted hook body or credential-bearing diagnostic.
        raise RuntimeError("GitHub API request failed. Check gh auth status and repository administration access.")
    return json.loads(result.stdout) if result.stdout.strip() else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    if os.getenv("DEPLOY_REPOSITORY", REPOSITORY).rstrip("/") != REPOSITORY:
        raise ValueError("DEPLOY_REPOSITORY must name the configured project repository.")
    secret = settings.github_webhook_secret
    if len(secret) < 32:
        raise ValueError("Set a random GITHUB_WEBHOOK_SECRET of at least 32 characters in .env.")
    expected = settings.public_base_url + "/github/webhook"
    # GET follows repository rename redirects; GitHub rejects PATCH/POST on
    # the old name with 301. Resolve the current name before mutating a hook.
    repository = api(f"repos/{REPOSITORY}").get("full_name", "")
    if (not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository)
            or repository.split("/")[-1] in {".", ".."}):
        raise ValueError("GitHub did not return a valid repository name.")
    hooks = api(f"repos/{repository}/hooks?per_page=100")
    saved = json.loads(STATE.read_text()) if STATE.exists() else {}
    current = next((h for h in hooks if h["id"] == saved.get("id")), None)
    if current is None:
        matches = [h for h in hooks if h.get("config", {}).get("url") == expected]
        if len(matches) > 1:
            raise ValueError("Multiple matching GitHub webhooks exist; inspect them before changing configuration.")
        current = matches[0] if matches else None
        if current and current.get("events") != ["push"]:
            raise ValueError("An unowned webhook at this URL has other subscriptions. Configure a dedicated push hook first.")
    if args.apply:
        data = {"name": "web", "active": True, "events": ["push"],
                "config": {"url": expected, "content_type": "json", "insecure_ssl": "0", "secret": secret}}
        path = f"repos/{repository}/hooks"
        if current:
            current = api(path + "/" + str(current["id"]), "PATCH", data)
        else:
            current = api(path, "POST", data)
        STATE.parent.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump({"id": current["id"], "repository": repository}, handle)
        current = api(f"repos/{repository}/hooks/{current['id']}")
    matches = bool(current and current["active"] and current["events"] == ["push"]
                   and current.get("config", {}).get("url") == expected
                   and current.get("config", {}).get("content_type") == "json"
                   and str(current.get("config", {}).get("insecure_ssl", "0")) == "0")
    print(json.dumps({"repository": repository, "hook_id": current["id"] if current else None,
                      "url": current.get("config", {}).get("url") if current else None,
                      "matches_local_tunnel": matches}, indent=2))
    if args.apply and not matches:
        raise RuntimeError("GitHub webhook read-back did not match the requested configuration.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
