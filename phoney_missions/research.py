"""Curated public research. This module does not call X and does not fetch secrets."""

from __future__ import annotations

import json
from pathlib import Path

from .schema import MissionPack, MissionSchemaError
from .secrets import Secret, load_secret

REPO = Path(__file__).resolve().parents[1]
CACHE = Path(__file__).resolve().parent / "cache"


class ResearchError(ValueError):
    """Research could not be written. The message names env vars, not values."""


def _repo_path(relative: str) -> Path:
    path = (REPO / relative).resolve()
    if REPO not in path.parents and path != REPO:
        raise ResearchError("Research path escapes the repository.")
    return path


def load_utterances(pack: MissionPack) -> list[dict]:
    if not pack.utterances_doc:
        return []
    path = _repo_path(pack.utterances_doc)
    if not path.is_file():
        return []
    document = json.loads(path.read_text("utf-8"))
    rows = document.get("lines") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ResearchError("Utterance bank lines must be a list.")
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("text"), str)]


def _configured_secrets(environ) -> list[Secret]:
    source = environ or {}
    names = ("CANCEL_ACCOUNT_PIN", "CANCEL_SSN_LAST4")
    return [load_secret(source, name) for name in names]


def assert_research_clean(text: str, environ=None) -> None:
    leaked = [secret.env_name for secret in _configured_secrets(environ) if secret.leaked_into(text)]
    if leaked:
        raise ResearchError("refusing to write research that contains " + ", ".join(leaked))


def build_cache(pack: MissionPack, environ=None) -> dict:
    """Machine-readable copy of the curated public notes. No network."""
    doc_text = ""
    if pack.research_doc:
        path = _repo_path(pack.research_doc)
        if path.is_file():
            doc_text = path.read_text("utf-8")
    utterances = load_utterances(pack)
    payload = {
        "mission_id": pack.id,
        "public": True,
        "contains_secrets": False,
        "x_refresh_after": pack.x_refresh_after,
        "x_todo": (
            "X post search hit the daily read limit on 2026-10-28. "
            "Refresh public posts after 2026-10-29T00:00:00Z. Do not add secrets or legal claims."
        ),
        "research_doc": pack.research_doc,
        "utterance_ids": [str(row.get("id", "")) for row in utterances],
        "utterances": [
            {"id": str(row.get("id", "")), "when": str(row.get("when", "")), "text": row["text"]}
            for row in utterances
        ],
    }
    blob = doc_text + "\n" + json.dumps(payload)
    assert_research_clean(blob, environ)
    if "password" in doc_text.lower() and "never" not in doc_text.lower():
        raise ResearchError("research document looks like it contains a password.")
    return payload


def write_cache(pack: MissionPack, dest: Path, environ=None) -> Path:
    payload = build_cache(pack, environ)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n", "utf-8")
    assert_research_clean(dest.read_text("utf-8"), environ)
    return dest


def default_cache_path(pack: MissionPack) -> Path:
    return CACHE / f"{pack.id}.json"
