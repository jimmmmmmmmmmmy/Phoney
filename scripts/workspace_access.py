#!/usr/bin/env python3
"""Configure the shared PIN locally; credentials are never command arguments."""

import argparse
import getpass
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from workspace_auth import AccessNotConfigured, AccessUnavailable, WorkspaceAccess


def main(argv=None):
    parser = argparse.ArgumentParser(description="Set or revoke Phoney shared workspace access.")
    parser.add_argument("command", choices=("set", "status", "revoke"))
    parser.add_argument("--workspace", help="Workspace ID (defaults to WORKSPACE_ID or default).")
    parser.add_argument("--env-file", help="Absolute path to the serving deployment's .env file.")
    args = parser.parse_args(argv)
    env_file = Path(args.env_file) if args.env_file else ROOT / ".env"
    if args.env_file and (not env_file.is_absolute() or not env_file.is_file()):
        parser.error("--env-file must point to an existing absolute .env path.")
    load_dotenv(env_file, override=bool(args.env_file))
    try:
        access = WorkspaceAccess(os.getenv("WORKSPACE_STORAGE_DIR", "").strip(),
                                 os.getenv("DATABASE_URL", "").strip(),
                                 args.workspace or os.getenv("WORKSPACE_ID", "default").strip())
        if args.command == "set":
            if not sys.stdin.isatty():
                parser.error("Run set in an interactive terminal; credentials must not be echoed or piped.")
            pin = getpass.getpass("New shared six-digit PIN: ")
            if pin != getpass.getpass("Confirm PIN: "):
                parser.error("PIN confirmation does not match.")
            password = getpass.getpass("New recovery password (at least 15 characters): ")
            if password != getpass.getpass("Confirm recovery password: "):
                parser.error("Password confirmation does not match.")
            access.configure(pin, password)
            print("Workspace access configured. Previous remembered devices have been revoked.")
        elif args.command == "revoke":
            access.revoke()
            print("All remembered devices for this workspace have been revoked.")
        else:
            try:
                access.status(None, "local-status")
                print("Workspace access credentials are configured.")
            except AccessNotConfigured:
                print("Workspace access credentials have not been configured.")
        return 0
    except (ValueError, AccessUnavailable) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
