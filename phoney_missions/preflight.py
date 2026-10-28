"""Refuse a live unattended dial that would have to invent a secret."""

from __future__ import annotations

from .brief import resolve_identity
from .gate import unattended_dial_permitted
from .schema import MissionPack
from .secrets import load_secret


def missing_unattended(pack: MissionPack, environ=None) -> list[str]:
    """Names only. Values are not included."""
    import os
    source = os.environ if environ is None else environ
    required = pack.unattended_requires or (
        "account_holder_name", "account_number", "lines", "pin", "ssn_last4")
    holder, account, lines = resolve_identity(pack, source)
    missing = []
    for item in required:
        if item in {"account_holder_name", "account_name"} and not holder:
            missing.append("account_holder_name")
        elif item == "account_number" and not account:
            missing.append("account_number")
        elif item == "lines" and len(lines) < 1:
            missing.append("lines")
        elif item in pack.secret_env:
            if not load_secret(source, pack.secret_env[item]).configured:
                missing.append(pack.secret_env[item])
        elif item in {"pin", "ssn_last4"} and item not in pack.secret_env:
            missing.append(item)
    return missing


def preflight_block(pack: MissionPack, environ=None, *, dial: bool) -> str | None:
    """Block unattended live prep, and every place-call, when cover facts are missing."""
    import os
    source = os.environ if environ is None else environ
    unattended, _ = unattended_dial_permitted(source, mission=True)
    if not dial and not unattended:
        return None
    if dial and not unattended and not missing_unattended(pack, source):
        return None
    missing = missing_unattended(pack, source)
    if not missing:
        return None
    if dial or unattended:
        return "Refusing to dial. Missing " + ", ".join(missing) + "."
    return None
