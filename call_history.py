"""Page the saved call catalog without turning live polling into an archive dump."""

import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import re

from call_details import normalize_caller_number

PAGE_SIZE = 20
SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")


def sort_key(row):
    value = row.get("started_at") or row.get("ended_at") or row.get("finished_at")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        value = stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError, OverflowError):
        value = ""
    return value, row["call_sid"]


def duration(row):
    seconds = row.get("duration_seconds")
    if type(seconds) in (int, float) and math.isfinite(seconds) and 0 <= seconds <= 86400:
        return seconds
    try:
        start = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat((row.get("ended_at") or row["finished_at"]).replace("Z", "+00:00"))
        seconds = (end - start).total_seconds()
        return seconds if 0 <= seconds <= 86400 else 0
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return 0


def decode_cursor(cursor, caller):
    if not cursor:
        return None
    try:
        if len(cursor) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        key = data["before"]
        if (set(data) != {"v", "caller", "before"} or data["v"] != 1 or data["caller"] != caller
                or not isinstance(key, list) or len(key) != 2
                or not all(isinstance(value, str) for value in key)
                or len(key[0]) > 40 or not SID.fullmatch(key[1])):
            raise ValueError
        return tuple(key)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError("Invalid call history cursor") from None


def encode_cursor(key, caller):
    data = json.dumps({"v": 1, "caller": caller, "before": key}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def saved_call(manager, call_sid, hot_sessions):
    current = next((item for item in hot_sessions if item["call_sid"] == call_sid), None)
    getter = getattr(manager, "get_saved_call", None)
    # Live and failed-to-save results belong to this process. Successfully
    # persisted calls must reflect disk corrections/deletions even while hot.
    if current is not None and (getter is None or call_sid in getattr(manager, "sessions", {})
                                or not current.get("ended_at") or current.get("storage_error")):
        return deepcopy(current)
    return getter(call_sid) if getter else None


def details_for(store, call_sid, document=None):
    if store is None:
        return None
    getter = getattr(store, "get", None)
    if getter:
        return getter(call_sid, document)
    return next((item for item in store.snapshot([document] if document else [])["calls"]
                 if item["call_sid"] == call_sid), None)


def page(manager, details_store, recording_library, snapshot, voicemails, *, cursor=None,
         caller=None, call_sid=None):
    """Run file/index access on a worker thread. Full transcripts stay page-bounded."""
    caller = normalize_caller_number(caller) if caller else ""
    before = decode_cursor(cursor, caller)
    hot = snapshot["sessions"]
    archive = manager.archive_snapshot() if hasattr(manager, "archive_snapshot") else {"sessions": []}
    details = ({"enabled": False, "storage_error": "", "calls": []} if details_store is None else
               details_store.archive_snapshot() if hasattr(details_store, "archive_snapshot") else
               details_store.snapshot(hot))
    recordings = ({"enabled": False, "storage_error": "", "recordings": []} if recording_library is None else
                  recording_library.archive_snapshot() if hasattr(recording_library, "archive_snapshot") else
                  recording_library.snapshot())
    archived_ids = {item["call_sid"] for item in archive["sessions"]}
    current_rows = [item for item in hot if item["call_sid"] not in archived_ids
                    or item["call_sid"] in getattr(manager, "sessions", {})
                    or not item.get("ended_at") or item.get("storage_error")]
    sources = [voicemails.get("voicemails", []), recordings["recordings"], archive["sessions"],
               current_rows, details["calls"]]
    rows = {}
    for source in sources:
        for item in source:
            sid = item.get("call_sid")
            if not isinstance(sid, str) or not SID.fullmatch(sid):
                continue
            rows[sid] = {**rows.get(sid, {}), **{key: value for key, value in item.items()
                                              if key in {"call_sid", "started_at", "ended_at", "finished_at",
                                                         "caller_number", "duration_seconds"}
                                              and value is not None}}
    matched = sorted((row for row in rows.values()
                      if not caller or row.get("caller_number") == caller), key=sort_key, reverse=True)
    candidates = [row for row in matched if before is None or sort_key(row) < before]
    selected_rows = candidates[:PAGE_SIZE]
    ids = [row["call_sid"] for row in selected_rows]
    more = len(candidates) > PAGE_SIZE
    caller_metrics = {}
    for row in matched:
        number = row.get("caller_number")
        if not number:
            continue
        metrics = caller_metrics.setdefault(number, {"total": 0, "duration_seconds": 0,
                                                       "last_contact_at": row.get("started_at")})
        metrics["total"] += 1
        metrics["duration_seconds"] = round(metrics["duration_seconds"] + duration(row), 3)
    snapshot["history"] = {
        "next_cursor": encode_cursor(sort_key(selected_rows[-1]), caller) if more else None,
        "has_more": more, "total": len(matched),
        "duration_seconds": round(sum(duration(row) for row in matched), 3),
        "last_contact_at": matched[0].get("started_at") if matched else None,
        "caller_metrics": caller_metrics,
        "complete": not any(item.get("storage_error") for item in (archive, details, recordings, voicemails)),
    }
    requested = call_sid if isinstance(call_sid, str) and SID.fullmatch(call_sid) else None
    # Caller filtering must never hide a different caller's incoming alert.
    ids.extend(item["call_sid"] for item in hot if not item.get("ended_at") and item["call_sid"] not in ids)
    documents = {}
    for sid in ids + ([requested] if requested and requested not in ids else []):
        document = saved_call(manager, sid, hot)
        if document:
            documents[sid] = document
    # Direct lookups work even before an index refresh or beyond old cache limits.
    if requested and requested not in ids:
        ids.append(requested)
    page_details = []
    for sid in ids:
        item = details_for(details_store, sid, documents.get(sid))
        if item:
            page_details.append(item)
    page_recordings = [item for item in recordings["recordings"] if item["call_sid"] in ids]
    if requested and not any(item["call_sid"] == requested for item in page_recordings):
        getter = getattr(recording_library, "get", None)
        item = getter(requested) if getter else None
        if item:
            page_recordings.append(item)
    snapshot["sessions"] = list(documents.values())
    snapshot["call_details"] = {**details, "calls": page_details}
    snapshot["recordings"] = {**recordings, "recordings": page_recordings}
    snapshot["voicemail"] = {**voicemails, "voicemails": [item for item in voicemails["voicemails"]
                                                        if item["call_sid"] in ids]}
    available = set(documents) | {item["call_sid"] for item in page_details + page_recordings
                                 + snapshot["voicemail"]["voicemails"]}
    active = [item["call_sid"] for item in documents.values() if not item.get("ended_at")]
    selected = (requested if requested in available else None) if requested else next(
        iter(active or [sid for sid in ids if sid in available]), None)
    snapshot["selected_call_sid"] = selected
    snapshot["selected_call_missing"] = bool(requested and requested not in available)
    return snapshot
