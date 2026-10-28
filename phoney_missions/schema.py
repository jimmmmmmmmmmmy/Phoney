"""Generic mission packs. T-Mobile cancel is one instance, not the schema."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_E164 = re.compile(r"\+[1-9][0-9]{7,14}\Z")
_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_ACCOUNT = re.compile(r"[0-9]{6,12}\Z")
_LINE = re.compile(r"([0-9]{10}):([^,]{1,40})")
TOOLS = frozenset({"speak", "dtmf", "hangup", "note_confirmation"})
ROOT = Path(__file__).resolve().parent
PACKS = ROOT / "packs"
TEMPLATE = ROOT / "templates" / "mission.json"

_SECRET_KEYS = frozenset({
    "password", "pin", "ssn", "ssn_last4", "account_pin", "passcode",
})


class MissionSchemaError(ValueError):
    """The pack is not usable. The message never includes a secret value."""


@dataclass(frozen=True)
class Line:
    digits: str
    label: str

    @property
    def display(self) -> str:
        return f"{self.digits[:3]}-{self.digits[3:6]}-{self.digits[6:]}"

    @property
    def last4(self) -> str:
        return self.digits[-4:]


@dataclass(frozen=True)
class RetentionPolicy:
    accept_if: str
    min_monthly_reduction_cents: int
    min_months: int
    decline_one_time_credits: bool


@dataclass(frozen=True)
class IvrEntry:
    id: str
    phrases: tuple[str, ...]
    auto: bool
    digits: str
    priority: int


@dataclass(frozen=True)
class MissionPack:
    id: str
    version: int
    title: str
    destination_e164: str
    destination_display: str
    goal_key: str
    aliases: tuple[str, ...]
    spoken_goal: str
    account_name: str
    account_number: str
    lines: tuple[Line, ...]
    identity_env: dict[str, str]
    secret_env: dict[str, str]
    constraints: tuple[str, ...]
    prefer: str
    brevity: str
    retention: RetentionPolicy
    forbidden: tuple[str, ...]
    tools: tuple[str, ...]
    brief_template: str
    ivr: tuple[IvrEntry, ...]
    research_doc: str
    utterances_doc: str
    x_refresh_after: str

    def line_text(self) -> str:
        return "; ".join(f"{line.label} {line.display}" for line in self.lines)


def _reject_password_keys(value, where: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"password", "passwd"} or "password" in str(key).lower():
                raise MissionSchemaError(f"{where}.{key} is not allowed. Passwords are never loaded.")
            _reject_password_keys(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_password_keys(item, f"{where}[{index}]")


def _env_name(value, label: str) -> str:
    if not isinstance(value, dict) or set(value) != {"env"} or not isinstance(value.get("env"), str):
        raise MissionSchemaError(f"{label} must be an object with an env name, not a literal value.")
    name = value["env"].strip()
    if not _ENV.fullmatch(name) or "PASSWORD" in name:
        raise MissionSchemaError(f"{label} env name is not allowed.")
    return name


def _lines(raw) -> tuple[Line, ...]:
    if not isinstance(raw, list) or not raw:
        raise MissionSchemaError("identity.lines must be a non-empty list.")
    lines = []
    for row in raw:
        if not isinstance(row, dict):
            raise MissionSchemaError("each line must be an object.")
        digits = str(row.get("digits", ""))
        label = str(row.get("label", "")).strip()
        if not re.fullmatch(r"[0-9]{10}", digits) or not label or len(label) > 40:
            raise MissionSchemaError("each line needs a 10-digit number and a short label.")
        lines.append(Line(digits, label))
    return tuple(lines)


def parse_lines_env(raw: str) -> tuple[Line, ...]:
    """``CANCEL_LINES`` form: ``10-digits:label`` pairs, comma-separated."""
    matches = list(_LINE.finditer(raw))
    if not matches or len(matches) != raw.count(",") + 1:
        raise MissionSchemaError("CANCEL_LINES must be comma-separated 10-digit:label pairs.")
    return tuple(Line(match.group(1), match.group(2).strip()) for match in matches)


def parse_pack(data: dict) -> MissionPack:
    if not isinstance(data, dict):
        raise MissionSchemaError("A mission pack must be a JSON object.")
    _reject_password_keys(data, "pack")
    for key in _SECRET_KEYS:
        if key in data and not isinstance(data[key], dict):
            raise MissionSchemaError(f"{key} must be an env reference, not a literal value.")
    ident = str(data.get("id", ""))
    if not _ID.fullmatch(ident):
        raise MissionSchemaError("Mission id must be a short lowercase slug.")
    if data.get("version") != 1:
        raise MissionSchemaError("Only mission pack version 1 is supported.")
    destination = data.get("destination")
    if not isinstance(destination, dict) or not _E164.fullmatch(str(destination.get("e164", ""))):
        raise MissionSchemaError("destination.e164 must be an E.164 number.")
    identity = data.get("identity")
    if not isinstance(identity, dict):
        raise MissionSchemaError("identity must be an object.")
    account = str(identity.get("account_number", ""))
    if not _ACCOUNT.fullmatch(account):
        raise MissionSchemaError("identity.account_number must be 6 to 12 digits.")
    name = str(identity.get("account_name", "")).strip()
    if not name or len(name) > 80:
        raise MissionSchemaError("identity.account_name must be a short name.")
    policy = data.get("policy")
    if not isinstance(policy, dict):
        raise MissionSchemaError("policy must be an object.")
    brevity = str(data.get("brevity", ""))
    if brevity not in {"strict", "normal"}:
        raise MissionSchemaError("brevity must be strict or normal.")
    retention = policy.get("retention")
    if not isinstance(retention, dict):
        raise MissionSchemaError("policy.retention must be an object.")
    cents = retention.get("min_monthly_reduction_cents")
    months = retention.get("min_months")
    if type(cents) is not int or isinstance(cents, bool) or not 0 <= cents <= 100_000:
        raise MissionSchemaError("min_monthly_reduction_cents must be a small integer.")
    if type(months) is not int or isinstance(months, bool) or not 1 <= months <= 36:
        raise MissionSchemaError("min_months must be an integer from 1 to 36.")
    if retention.get("accept_if") != "lasting_bill_cut":
        raise MissionSchemaError("retention.accept_if must be lasting_bill_cut.")
    tools = data.get("tools")
    if not isinstance(tools, list) or not tools or any(tool not in TOOLS for tool in tools):
        raise MissionSchemaError("tools must list speak, dtmf, hangup, and note_confirmation.")
    ivr = data.get("ivr")
    rows = ivr.get("entries") if isinstance(ivr, dict) else None
    if not isinstance(rows, list) or not rows:
        raise MissionSchemaError("ivr.entries must be a non-empty list.")
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            raise MissionSchemaError("each IVR entry must be an object.")
        phrases = row.get("phrases")
        if not isinstance(phrases, list) or not phrases:
            raise MissionSchemaError("each IVR entry needs phrases.")
        entries.append(IvrEntry(
            str(row.get("id", "")).strip(),
            tuple(str(phrase) for phrase in phrases),
            bool(row.get("auto", False)),
            str(row.get("digits", "")),
            int(row.get("priority", 10)),
        ))
        if not entries[-1].id:
            raise MissionSchemaError("each IVR entry needs an id.")
    research = data.get("research") if isinstance(data.get("research"), dict) else {}
    secrets = data.get("secrets") if isinstance(data.get("secrets"), dict) else {}
    identity_env = identity.get("env") if isinstance(identity.get("env"), dict) else {}
    secret_env = {}
    for label, raw in secrets.items():
        if str(label).lower() in _SECRET_KEYS or label in {"pin", "ssn_last4"}:
            secret_env[str(label)] = _env_name(raw, f"secrets.{label}")
        else:
            raise MissionSchemaError(f"secrets.{label} is not a known secret slot.")
    env_names = {}
    for label, raw in identity_env.items():
        if label not in {"account_name", "account_number", "lines"}:
            raise MissionSchemaError(f"identity.env.{label} is not overridable.")
        env_names[label] = _env_name({"env": raw} if isinstance(raw, str) else raw, f"identity.env.{label}")
    brief = str(data.get("brief_template", "")).strip()
    if len(brief) < 20:
        raise MissionSchemaError("brief_template must describe the task.")
    goal = str(data.get("goal_key", "")).strip()
    if goal != f"mission:{ident}":
        raise MissionSchemaError("goal_key must be mission:<id>.")
    aliases = data.get("aliases", [])
    if not isinstance(aliases, list) or any(not isinstance(item, str) for item in aliases):
        raise MissionSchemaError("aliases must be a list of strings.")
    forbidden = policy.get("forbidden", [])
    constraints = data.get("constraints", [])
    if not isinstance(forbidden, list) or not isinstance(constraints, list):
        raise MissionSchemaError("constraints and policy.forbidden must be lists.")
    return MissionPack(
        id=ident,
        version=1,
        title=str(data.get("title", ident)).strip()[:80],
        destination_e164=str(destination["e164"]),
        destination_display=str(destination.get("display", destination["e164"]))[:40],
        goal_key=goal,
        aliases=tuple(aliases),
        spoken_goal=str(data.get("spoken_goal", "")).strip()[:300],
        account_name=name,
        account_number=account,
        lines=_lines(identity.get("lines")),
        identity_env=env_names,
        secret_env=secret_env,
        constraints=tuple(str(item) for item in constraints),
        prefer=str(policy.get("prefer", "")),
        brevity=brevity,
        retention=RetentionPolicy(
            accept_if="lasting_bill_cut",
            min_monthly_reduction_cents=cents,
            min_months=months,
            decline_one_time_credits=bool(retention.get("decline_one_time_credits", True)),
        ),
        forbidden=tuple(str(item) for item in forbidden),
        tools=tuple(str(tool) for tool in tools),
        brief_template=brief,
        ivr=tuple(entries),
        research_doc=str(research.get("document", "")),
        utterances_doc=str(research.get("utterances", "")),
        x_refresh_after=str(research.get("x_refresh_after", "")),
    )


def load_pack(mission_id: str) -> MissionPack:
    if not _ID.fullmatch(str(mission_id or "")):
        raise MissionSchemaError("Unknown mission.")
    path = PACKS / f"{mission_id}.json"
    if not path.is_file():
        raise MissionSchemaError(f"Unknown mission {mission_id}.")
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise MissionSchemaError("Mission pack is not valid JSON.") from exc
    return parse_pack(data)


def load_template() -> dict:
    return json.loads(TEMPLATE.read_text("utf-8"))
