#!/usr/bin/env python3
"""Open the URL-accessible transcript viewer for the installed server."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
import webbrowser

from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Use a specific private environment file")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    pointer = root / ".runtime/server-root.json"
    if args.env_file:
        env_file = args.env_file.expanduser()
    elif pointer.exists():
        host = Path.home() / "Library/Application Support/NewCollegeOperator"
        if Path(json.loads(pointer.read_text())["path"]) != host:
            parser.error("Unexpected installed server path")
        env_file = host / ".env"
    else:
        env_file = root / ".env"
    values = dotenv_values(env_file)
    origin = (values.get("PUBLIC_BASE_URL") or "").rstrip("/")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        parser.error("Configure PUBLIC_BASE_URL in the server environment")
    url = origin + "/dashboard"
    if not webbrowser.open(url):
        parser.error("The default browser could not be opened")
    print("Opened the call transcript dashboard.")


if __name__ == "__main__":
    main()
