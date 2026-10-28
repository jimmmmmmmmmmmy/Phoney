"""Write a call report from notes. This does not dial."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .persona import reveals


def _outcomes(notes: list[dict]) -> list[str]:
    lines = []
    for note in notes:
        kind = note.get("kind")
        if kind == "confirmation":
            lines.append(
                f"- {note.get('line')}: canceled, confirmation {note.get('confirmation')}, "
                f"effective {note.get('effective')}")
        elif kind == "retention":
            lines.append(
                f"- retention accepted: {note.get('monthly_cents')} cents/month "
                f"for {note.get('months')}")
        elif kind == "blocked":
            lines.append(f"- blocked:{note.get('reason', 'verification')}")
    return lines or ["- no line outcome recorded"]


def write_call_report(mission_id: str, *, notes: list[dict], spoken: list[str],
                      ended_reason: str, unattended: bool, destination: str,
                      duration_s: float, root: Path, simulated: bool) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = root / mission_id / stamp
    folder.mkdir(parents=True, exist_ok=True)
    cover = not any(reveals(line) for line in spoken)
    transcript = {
        "mission_id": mission_id,
        "simulated": simulated,
        "unattended": unattended,
        "destination": destination,
        "ended_reason": ended_reason,
        "duration_s": round(duration_s, 3),
        "spoken": spoken,
        "notes": notes,
        "cover_held": cover,
    }
    (folder / "transcript.json").write_text(json.dumps(transcript, indent=2) + "\n", "utf-8")
    (folder / "transcript.md").write_text(
        "\n".join(["# Transcript", "", *[f"- {line}" for line in spoken], ""]) , "utf-8")
    body = "\n".join([
        f"# Call report: {mission_id}",
        "",
        f"- Simulated: {str(simulated).lower()}",
        f"- Unattended: {str(unattended).lower()}",
        f"- Destination: {destination}",
        f"- Duration seconds: {round(duration_s, 3)}",
        f"- Ended: {ended_reason or 'open'}",
        f"- Cover held: {str(cover).lower()}",
        "",
        "## Outcome",
        *_outcomes(notes),
        "",
        "## Spoken lines",
        *[f"- {line}" for line in spoken],
        "",
        "## Next actions",
        "- If this file is from --simulate, it is not a live carrier result.",
        "- A live Mac run should replace this folder with the saved session transcript.",
        "",
    ])
    (folder / "CALL_REPORT.md").write_text(body, "utf-8")
    return folder
