"""Generate a local one-time dashboard unlock code, or revoke owner sessions.

This command never prints OPERATOR_ADMIN_TOKEN or provider keys. A code expires
in five minutes and can be redeemed once in the dashboard's New call dialog.
"""

import argparse
from pathlib import Path
import sys

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_registry import AgentRegistry, RegistryError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True,
                        help="Explicit server environment file with WORKSPACE_STORAGE_DIR")
    parser.add_argument("--revoke", action="store_true", help="Revoke every owner session and pending code")
    parser.add_argument("--expires-in", type=int, default=300, help="Grant lifetime in seconds (30–600)")
    args = parser.parse_args(argv)
    if not args.env_file.is_file():
        raise RegistryError("The server environment file was not found.")
    values = dotenv_values(args.env_file)
    if str(values.get("AGENT_MANAGEMENT_ENABLED", "")).lower() != "true" and not args.revoke:
        raise RegistryError("Enable AGENT_MANAGEMENT_ENABLED on the server before issuing an owner code.")
    registry = AgentRegistry(str(values.get("WORKSPACE_STORAGE_DIR") or "").strip())
    if args.revoke:
        registry.revoke_all()
        print("Owner sessions and pending access codes revoked.")
    else:
        code = registry.grant(ttl=args.expires_in)
        print(f"Paste this one-time code into New call → Unlock calling (expires in {args.expires_in} seconds):")
        print(code)


if __name__ == "__main__":
    try:
        main()
    except (RegistryError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
