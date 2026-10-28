"""Connect a mission pack to the live operator without teaching it T-Mobile only."""

from __future__ import annotations

import os

from voice_stack.ivr import load_playbook, playbook_instructions

from .brief import assemble_brief, resolve_identity
from .ivr_policy import playbook_entries
from .schema import PACKS, MissionSchemaError, load_pack

MISSION_AGENT_KIND = "phone-mission"
LEGACY_GOALS = {
    "cancel-tmobile": "tmobile-cancel",
    "Cancel only the two named T-Mobile lines, confirm each one, and leave every other line active.": "tmobile-cancel",
}


def mission_id_for_goal(goal: str) -> str | None:
    text = str(goal or "").strip()
    if text in LEGACY_GOALS:
        return LEGACY_GOALS[text]
    if text.startswith("mission:"):
        ident = text.split(":", 1)[1]
        return ident if _slug(ident) else None
    for path in PACKS.glob("*.json"):
        try:
            pack = load_pack(path.stem)
        except MissionSchemaError:
            continue
        if text in {pack.goal_key, *pack.aliases}:
            return pack.id
    return None


def _slug(ident: str) -> bool:
    from .schema import _ID
    return bool(_ID.fullmatch(ident))


def load_for_goal(goal: str):
    ident = mission_id_for_goal(goal)
    if ident is None:
        raise MissionSchemaError("Unknown mission.")
    return load_pack(ident)


def public_prompt_for(goal: str, environ=None) -> str:
    return assemble_brief(load_for_goal(goal), environ).public_text()


def mission_title(goal: str) -> str:
    return load_for_goal(goal).title


def spoken_goal_for(goal: str) -> str:
    pack = load_for_goal(goal)
    return pack.spoken_goal or pack.title


def brevity_for_goal(goal: str) -> str:
    try:
        return load_for_goal(goal).brevity
    except MissionSchemaError:
        return ""


def playbook_for_goal(goal: str, environ=None):
    pack = load_for_goal(goal)
    env = os.environ if environ is None else environ
    if pack.id == "tmobile-cancel" and str(env.get("TMOBILE_IVR_PLAYBOOK", "")).strip():
        return load_playbook(environ)
    return playbook_entries(pack)


def runtime_instruction_for(goal: str, playbook=None, environ=None) -> str:
    pack = load_for_goal(goal)
    brief = assemble_brief(pack, environ, include_research=False)
    book = playbook if playbook is not None else playbook_for_goal(goal, environ)
    return (
        "Trusted mission control. Say at most one short sentence unless you are "
        "confirming a number or a date. Prefer DTMF over speech when a digit works. "
        + brief.runtime_text()
        + "\n"
        + playbook_instructions(book)
    )


def facts_for(goal: str, environ=None) -> dict:
    """Account facts for the legacy cancel script. PIN and SSN are booleans in logs."""
    pack = load_for_goal(goal)
    name, account, lines = resolve_identity(pack, environ)
    brief = assemble_brief(pack, environ, include_research=False)
    return {
        "account_name": name,
        "account": account,
        "lines": [
            {"digits": line.digits, "display": line.display, "label": line.label}
            for line in lines
        ],
        "pin": "configured" if brief.secret_configured("pin") else "",
        "ssn_last4": "configured" if brief.secret_configured("ssn_last4") else "",
    }
