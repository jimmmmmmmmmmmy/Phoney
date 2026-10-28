"""Assemble a mission brief. Secrets stay out of the public text and out of repr."""

from __future__ import annotations

import os

from .brevity import instruction as brevity_instruction
from .policy import policy_text
from .research import load_utterances
from .schema import MissionPack, MissionSchemaError, parse_lines_env
from .secrets import Secret, assert_no_leak, load_secret

_TOOL_LINES = {
    "speak": "speak: one short sentence, then wait.",
    "dtmf": "dtmf: a [/DTMF digits] line, never spoken. Prefer it whenever a menu digit works.",
    "hangup": "hangup: say goodbye, then [/END CALL] on its own final line.",
    "note_confirmation": (
        "note_confirmation: [/NOTE confirm:NUMBER:CODE:YYYY-MM-DD] or "
        "[/NOTE retention:CENTS:MONTHS], never spoken."
    ),
}


class MissionBrief:
    """Public prompt plus a separate authentication block for the live model."""

    def __init__(self, pack: MissionPack, public: str, secrets: list[Secret], runtime: str):
        self.pack = pack
        self.mission_id = pack.id
        self._public = public
        self.secrets = secrets
        self._runtime = runtime

    def __repr__(self) -> str:
        return (
            f"MissionBrief(mission={self.mission_id}, public_chars={len(self._public)}, "
            "secrets=redacted)"
        )

    def public_text(self) -> str:
        return self._public

    def runtime_text(self) -> str:
        """Live model only. Do not log, persist, or put this in an assertion."""
        return self._runtime

    def secret_configured(self, slot: str) -> bool:
        env_name = self.pack.secret_env.get(slot, "")
        return any(secret.env_name == env_name and secret.configured for secret in self.secrets)

    def public_contains_configured_secrets(self) -> bool:
        return any(secret.leaked_into(self._public) for secret in self.secrets)

    def assert_public_safe(self) -> None:
        assert_no_leak(self._public, self.secrets)


def _env(environ, name: str) -> str:
    source = os.environ if environ is None else environ
    return str(source.get(name, "")).strip()


def resolve_identity(pack: MissionPack, environ=None) -> tuple[str, str, tuple]:
    name, account, lines = pack.account_name, pack.account_number, pack.lines
    mapping = pack.identity_env
    if "account_name" in mapping and _env(environ, mapping["account_name"]):
        name = _env(environ, mapping["account_name"])[:80]
    if "account_number" in mapping and _env(environ, mapping["account_number"]):
        account = _env(environ, mapping["account_number"])
        if not account.isdecimal() or not 6 <= len(account) <= 12:
            raise MissionSchemaError("CANCEL_ACCOUNT_NUMBER must be 6 to 12 digits.")
    if "lines" in mapping and _env(environ, mapping["lines"]):
        lines = parse_lines_env(_env(environ, mapping["lines"]))
    return name, account, lines


def _secrets(pack: MissionPack, environ) -> list[Secret]:
    source = os.environ if environ is None else environ
    return [load_secret(source, env_name) for env_name in pack.secret_env.values()]


def _authentication_public(secrets: list[Secret]) -> str:
    configured = [secret.env_name for secret in secrets if secret.configured]
    if configured:
        return (
            "The trusted runtime authentication block has the configured PIN or SSN last four. "
            "Say each at most once, only if asked. Do not volunteer them. "
            "If asked for anything else, say you need the account holder on the line and end the call. "
            "You do not have an account password. Never invent one, and never log one."
        )
    return (
        "You do not have the account PIN, passcode, SSN, or SSN last four. "
        "If the representative requires any of those, say you need the account holder "
        "on the line and end the call. Never invent a PIN, passcode, SSN, or last four. "
        "You do not have an account password. Never invent one, and never log one."
    )


def _authentication_runtime(pack: MissionPack, secrets: list[Secret]) -> str:
    """Included only in the per-reply instruction, not in the stored snapshot."""
    parts = ["Trusted authentication for this reply only."]
    by_env = {secret.env_name: secret for secret in secrets}
    any_value = False
    for slot, env_name in pack.secret_env.items():
        secret = by_env.get(env_name)
        if secret and secret.configured:
            any_value = True
            label = "account PIN" if slot == "pin" else "SSN last four"
            parts.append(f"{label} {secret.reveal()}.")
    if not any_value:
        parts.append("No PIN or SSN last four was provided.")
    parts.append("Do not repeat these values into a note, a log, or a research file.")
    return " ".join(parts)


def _research_lines(pack: MissionPack, name: str, lines, secrets: list[Secret]) -> list[str]:
    first = lines[0]
    values = {
        "line_last4": first.last4,
        "line_display": first.display,
        "line_label": first.label,
        "account_name": name,
    }
    spoken = []
    for row in load_utterances(pack):
        try:
            text = str(row.get("text", "")).format(**values)
        except (KeyError, IndexError, ValueError):
            continue
        text = " ".join(text.split())
        if not text or any(secret.leaked_into(text) for secret in secrets):
            continue
        spoken.append(text)
    return spoken


def assemble_brief(pack: MissionPack, environ=None, *, include_research: bool = True) -> MissionBrief:
    name, account, lines = resolve_identity(pack, environ)
    secrets = _secrets(pack, environ)
    line_text = "; ".join(f"{line.label} {line.display}" for line in lines)
    task = pack.brief_template.format(
        account_name=name,
        account_number=account,
        lines=line_text,
        destination_display=pack.destination_display,
    )
    parts = [task, _authentication_public(secrets), policy_text(pack)]
    if pack.brevity == "strict":
        parts.append(brevity_instruction())
    parts.append("Tools: " + " ".join(_TOOL_LINES[tool] for tool in pack.tools if tool in _TOOL_LINES))
    if pack.constraints:
        parts.append("Constraints: " + " ".join(pack.constraints))
    research = _research_lines(pack, name, lines, secrets) if include_research else []
    if research:
        listed = " | ".join(research)
        parts.append(
            "Allowed short lines, at most one per turn. These are tactics, not a script to read in full: "
            + listed
        )
    public = "\n".join(parts)
    if any(secret.leaked_into(public) for secret in secrets):
        raise MissionSchemaError("Refusing to build a public brief that contains a configured secret.")
    runtime = "\n".join([
        brevity_instruction() if pack.brevity == "strict" else "",
        policy_text(pack),
        _authentication_runtime(pack, secrets),
    ]).strip()
    return MissionBrief(pack, public, secrets, runtime)
