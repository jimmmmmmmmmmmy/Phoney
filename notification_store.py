"""A bounded, shared call-notification inbox in the durable workspace database."""

import json
import re

from call_history import sort_key
from workspace_store import WorkspaceError


SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
LIMIT = 20
RETAIN = 100


class NotificationStore:
    def __init__(self, workspace):
        self.workspace = workspace

    @staticmethod
    def _prepare(connection):
        connection.execute("CREATE TABLE IF NOT EXISTS call_notifications "
                           "(id TEXT PRIMARY KEY, payload TEXT NOT NULL, unread INTEGER NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS notification_state "
                           "(id INTEGER PRIMARY KEY CHECK(id=1))")

    def observe(self, calls, complete=True):
        """Reconcile trusted server call headers; never accept caller data from a browser."""
        rows = {}
        for call in calls:
            sid = call.get("call_sid")
            if not isinstance(sid, str) or not SID.fullmatch(sid):
                continue
            rows[sid] = {"id": sid, "phone": str(call.get("caller_number") or "")[:32],
                         "startedAt": str(call.get("started_at") or call.get("ended_at") or "")[:40],
                         "active": call.get("active") is True, "available": True,
                         "collection": "voicemail" if call.get("voicemail") else "recent"}
        ordered = sorted(rows.values(), key=lambda item: sort_key(
            {"call_sid": item["id"], "started_at": item["startedAt"]}), reverse=True)[:RETAIN]
        with self.workspace._transaction() as connection:
            self._prepare(connection)
            initialized = connection.execute("SELECT id FROM notification_state WHERE id=1").fetchone()
            existing = {sid: (payload, unread) for sid, payload, unread in connection.execute(
                "SELECT id, payload, unread FROM call_notifications")}
            if complete:
                for sid, (payload, _) in existing.items():
                    if sid in rows:
                        continue
                    item = json.loads(payload)
                    if item.get("active") or item.get("available"):
                        item.update(active=False, available=False)
                        connection.execute("UPDATE call_notifications SET payload=? WHERE id=?",
                                           (json.dumps(item, sort_keys=True, separators=(",", ":")), sid))
            for item in ordered:
                payload = json.dumps(item, sort_keys=True, separators=(",", ":"))
                old = existing.get(item["id"])
                if old is None:
                    # Existing saved history is available immediately without a
                    # flood of alerts. Calls arriving after initialization are unread.
                    connection.execute("INSERT INTO call_notifications VALUES (?, ?, ?)",
                                       (item["id"], payload, int(bool(initialized) or item["active"])))
                elif old[0] != payload:
                    connection.execute("UPDATE call_notifications SET payload=? WHERE id=?",
                                       (payload, item["id"]))
            connection.execute("INSERT OR IGNORE INTO notification_state VALUES (1)")
            entries = self._entries(connection)
            for item in entries[RETAIN:]:
                connection.execute("DELETE FROM call_notifications WHERE id=?", (item["id"],))
            return {"notifications": entries[:LIMIT]}

    @staticmethod
    def _entries(connection):
        entries = [{**json.loads(payload), "unread": bool(unread)} for payload, unread
                   in connection.execute("SELECT payload, unread FROM call_notifications")]
        return sorted(entries, key=lambda item: sort_key(
            {"call_sid": item["id"], "started_at": item["startedAt"]}), reverse=True)

    def mark_read(self, payload):
        ids = payload.get("ids")
        if (set(payload) != {"ids"} or not isinstance(ids, list) or len(ids) > LIMIT
                or not all(isinstance(sid, str) and SID.fullmatch(sid) for sid in ids)):
            raise WorkspaceError("Choose valid call notifications to mark as read.")
        with self.workspace._transaction() as connection:
            self._prepare(connection)
            connection.executemany("UPDATE call_notifications SET unread=0 WHERE id=?", [(sid,) for sid in ids])
            return {"notifications": self._entries(connection)[:LIMIT]}
