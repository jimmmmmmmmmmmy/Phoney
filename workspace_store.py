"""Private transactional storage for the public dashboard's shared workspace."""

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from urllib.parse import urlsplit

from postgres_store import PostgresUnavailable, transaction as postgres_transaction


MAX_CONTACTS = 500
MAX_AGENTS = 50
DATABASE_NAME = "workspace.sqlite3"
DEMO_PHONES = {
    "demo-alex-morgan": "+19415550101",
    "demo-maya-patel": "+19415550102",
    "demo-casey-reed": "+19415550103",
    "demo-jordan-ellis": "+19415550104",
}
LOCAL_ID = re.compile(r"local-[A-Za-z0-9-]{8,80}\Z")
AGENT_ID = re.compile(r"agent-[A-Za-z0-9-]{8,80}\Z")
PHONE = re.compile(r"\+[1-9][0-9]{7,14}\Z")
CONTACT_FIELDS = {"id", "firstName", "lastName", "phone", "email", "address", "website",
                  "company", "createdAt", "status", "labels", "demo", "note", "revision"}
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# POSIX locks can be released by closing another descriptor for the same inode.
# Keep filesystem validation and SQLite transactions together across local instances.
WORKSPACE_LOCK = threading.RLock()


class WorkspaceError(Exception):
    status_code = 400


class WorkspaceConflict(WorkspaceError):
    status_code = 409


class WorkspaceUnavailable(WorkspaceError):
    status_code = 503


def _text(value, field, limit, *, required=False, multiline=False):
    if not isinstance(value, str) or len(value) > limit:
        raise WorkspaceError(f"{field} must be text of at most {limit} characters.")
    text = value.strip()
    if ((required and not text) or any(ord(c) < 32 and not (multiline and c in "\n\t\r") for c in text)
            or any(0xD800 <= ord(c) <= 0xDFFF for c in text)):
        raise WorkspaceError(f"Enter a valid {field}.")
    return text


def _date(value, *, allow_empty=False):
    text = _text(value, "createdAt", 40, required=not allow_empty)
    if not text and allow_empty:
        return ""
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.+)?", text):
            raise ValueError
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise WorkspaceError("Enter a valid createdAt date.") from None
    return text


def _revision(value, *, default=0):
    revision = value.get("revision", default)
    if type(revision) is not int or not 0 <= revision <= 2**53 - 1:
        raise WorkspaceError("Enter a valid record revision.")
    return revision


def _saved_revision(raw):
    try:
        if not isinstance(raw, str) or len(raw) > 50000:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        revision = _revision(value, default=1)
        if not 1 <= revision < 2**53 - 1:
            raise ValueError
        return revision
    except (WorkspaceError, ValueError, TypeError, RecursionError):
        raise WorkspaceUnavailable("Workspace record revision is invalid. Changes were not saved.") from None


def _contact(value, record_id=None):
    if not isinstance(value, dict) or set(value) - CONTACT_FIELDS:
        raise WorkspaceError("Enter a valid contact record.")
    ident = value.get("id")
    if (not isinstance(ident, str) or (ident not in DEMO_PHONES and not LOCAL_ID.fullmatch(ident))
            or (record_id is not None and ident != record_id)):
        raise WorkspaceError("Enter a valid matching contact ID.")
    if "demo" in value and type(value["demo"]) is not bool:
        raise WorkspaceError("Enter a valid contact record.")
    if "note" in value:
        _text(value["note"], "note", 400, multiline=True)
    phone = _text(value.get("phone"), "phone", 60, required=True)
    phone = re.sub(r"[\s().-]", "", phone)
    if not PHONE.fullmatch(phone):
        raise WorkspaceError("Enter a phone number with a + country code.")
    result = {"id": ident,
              "firstName": _text(value.get("firstName"), "firstName", 80, required=True),
              "lastName": _text(value.get("lastName"), "lastName", 80, required=True),
              "phone": phone, "createdAt": _date(value.get("createdAt"))}
    for field in ("email", "address", "website", "company"):
        result[field] = _text(value.get(field, ""), field, 400, multiline=field == "address")
    website = result["website"]
    if website:
        try:
            parsed = urlsplit(website)
            if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or any(c.isspace() for c in website)
                    or "\\" in website):
                raise ValueError
            parsed.port
        except ValueError:
            raise WorkspaceError("Website must be a valid HTTP or HTTPS URL.") from None
    result["status"] = value.get("status", "New")
    if result["status"] not in ("New", "Active", "Follow up"):
        raise WorkspaceError("Choose a valid contact status.")
    labels = value.get("labels", [])
    if not isinstance(labels, list) or len(labels) > 10:
        raise WorkspaceError("Use at most 10 contact labels.")
    result["labels"] = list(dict.fromkeys(_text(label, "label", 40, required=True) for label in labels))
    result["demo"] = ident in DEMO_PHONES
    result["revision"] = _revision(value, default=1)
    return result


def _agent(value, record_id=None, *, importing=False):
    if not isinstance(value, dict) or set(value) - {"id", "name", "prompt", "createdAt", "revision"}:
        raise WorkspaceError("Enter a valid agent draft.")
    result = {"name": _text(value.get("name"), "name", 80, required=True),
              "prompt": _text(value.get("prompt", ""), "prompt", 8000, multiline=True),
              "createdAt": _date(value.get("createdAt", "") if importing else value.get("createdAt"),
                                 allow_empty=importing)}
    fingerprint = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    ident = value.get("id")
    if importing and ident is None:
        ident = "agent-import-" + fingerprint[:40]
    if (not isinstance(ident, str) or not AGENT_ID.fullmatch(ident)
            or (record_id is not None and ident != record_id)):
        raise WorkspaceError("Enter a valid matching agent ID.")
    return {"id": ident, **result, "revision": _revision(value, default=1)}, fingerprint


def _directory(path):
    if not path.is_absolute() or path == Path(path.anchor) or ".." in path.parts:
        raise WorkspaceUnavailable("Workspace storage is unavailable.")
    parent = os.open(path.anchor, DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, DIR_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(part, DIR_FLAGS, dir_fd=parent)
            os.close(parent)
            parent = child
        os.fchmod(parent, 0o700)
        return parent
    except BaseException:
        os.close(parent)
        raise


class WorkspaceStore:
    """Each operation uses durable, serialized SQLite or PostgreSQL storage."""

    def __init__(self, storage_dir, *, database_url="", workspace_id="default"):
        self.enabled = bool(storage_dir or database_url)
        self.path = Path(storage_dir) if storage_dir else None
        self.database_url = database_url
        self.workspace_id = workspace_id

    @contextmanager
    def _transaction(self):
        if self.database_url:
            try:
                with postgres_transaction(self.database_url, self.workspace_id) as connection:
                    yield connection
            except PostgresUnavailable:
                raise WorkspaceUnavailable("Workspace storage is unavailable. Changes were not saved.") from None
            return
        with WORKSPACE_LOCK:
            with self._locked_transaction() as connection:
                yield connection

    @contextmanager
    def _locked_transaction(self):
        if not self.enabled:
            raise WorkspaceUnavailable("Shared workspace storage is disabled.")
        root = connection = None
        try:
            root = _directory(self.path)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            try:
                fd = os.open(DATABASE_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root)
            except FileExistsError:
                fd = os.open(DATABASE_NAME, flags, dir_fd=root)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkspaceUnavailable("Workspace storage is unavailable.")
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    sidecar = os.open(DATABASE_NAME + suffix,
                                      os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root)
                except FileNotFoundError:
                    continue
                try:
                    info = os.fstat(sidecar)
                    # Another process may finish its transaction and unlink the
                    # journal after our open; that detached inode is harmless.
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                        raise WorkspaceUnavailable("Workspace storage is unavailable.")
                    os.fchmod(sidecar, 0o600)
                finally:
                    os.close(sidecar)
            connection = sqlite3.connect(str(self.path / DATABASE_NAME), timeout=5, isolation_level=None)
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise WorkspaceUnavailable("Workspace storage version is unsupported.")
            connection.execute("CREATE TABLE IF NOT EXISTS contacts (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                               "phone TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, "
                               "payload TEXT NOT NULL)")
            connection.execute("PRAGMA user_version=1")
            yield connection
            connection.commit()
        except WorkspaceError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
            raise WorkspaceUnavailable("Workspace storage is unavailable. Changes were not saved.") from None
        finally:
            if connection is not None:
                connection.close()  # Rolls back an uncommitted transaction.
            if root is not None:
                os.close(root)

    @staticmethod
    def _snapshot(connection):
        snapshot = {"version": 1, "contacts": [], "demoOverrides": [], "agents": []}
        # PostgreSQL records have an explicit insertion sequence; preserve the
        # established SQLite insertion order without rewriting legacy files.
        order = getattr(connection, "insertion_order", "rowid")
        rows = connection.execute(f"SELECT id,kind,phone,payload FROM contacts ORDER BY {order} LIMIT ?",
                                  (MAX_CONTACTS + len(DEMO_PHONES) + 1,)).fetchall()
        for ident, kind, phone, raw in rows:
            if not isinstance(raw, str) or len(raw) > 8000:
                raise WorkspaceUnavailable("Workspace contact data is invalid.")
            try:
                record = _contact(json.loads(raw), ident)
            except WorkspaceError:
                raise WorkspaceUnavailable("Workspace contact data is invalid.") from None
            expected = "demo" if ident in DEMO_PHONES else "local"
            if kind != expected or phone != record["phone"]:
                raise WorkspaceUnavailable("Workspace contact data is invalid.")
            snapshot["demoOverrides" if kind == "demo" else "contacts"].append(record)
        agents = connection.execute(f"SELECT id,fingerprint,payload FROM agents ORDER BY {order} LIMIT ?", (MAX_AGENTS + 1,)).fetchall()
        for ident, fingerprint, raw in agents:
            if not isinstance(raw, str) or len(raw) > 50000:
                raise WorkspaceUnavailable("Workspace agent data is invalid.")
            try:
                record, actual = _agent(json.loads(raw), ident, importing=True)
            except WorkspaceError:
                raise WorkspaceUnavailable("Workspace agent data is invalid.") from None
            if actual != fingerprint:
                raise WorkspaceUnavailable("Workspace agent data is invalid.")
            snapshot["agents"].append(record)
        if (len(snapshot["contacts"]) > MAX_CONTACTS or len(snapshot["demoOverrides"]) > len(DEMO_PHONES)
                or len(snapshot["agents"]) > MAX_AGENTS):
            raise WorkspaceUnavailable("Workspace capacity is invalid.")
        return snapshot

    def snapshot(self):
        with self._transaction() as connection:
            return self._snapshot(connection)

    @staticmethod
    def _phone_exists(connection, phone, ident):
        if connection.execute("SELECT 1 FROM contacts WHERE phone=? AND id!=?", (phone, ident)).fetchone():
            return True
        return any(demo_id != ident and demo_phone == phone
                   and not connection.execute("SELECT 1 FROM contacts WHERE id=?", (demo_id,)).fetchone()
                   for demo_id, demo_phone in DEMO_PHONES.items())

    @staticmethod
    def _save_contact(connection, record, *, expected_revision=None):
        ident = record["id"]
        if WorkspaceStore._phone_exists(connection, record["phone"], ident):
            raise WorkspaceConflict("A contact with this phone number already exists.")
        kind = "demo" if ident in DEMO_PHONES else "local"
        exists = connection.execute("SELECT payload FROM contacts WHERE id=?", (ident,)).fetchone()
        current_revision = _saved_revision(exists[0]) if exists else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise WorkspaceConflict("This contact changed in another browser. Your draft is unchanged. Close and reopen the editor to review the latest details before saving.")
        record["revision"] = current_revision + 1
        if not exists and connection.execute("SELECT count(*) FROM contacts WHERE kind=?", (kind,)).fetchone()[0] >= (
                len(DEMO_PHONES) if kind == "demo" else MAX_CONTACTS):
            raise WorkspaceConflict("The workspace contact limit has been reached.")
        connection.execute("INSERT INTO contacts(id,kind,phone,payload) VALUES(?,?,?,?) "
                           "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,phone=excluded.phone,payload=excluded.payload",
                           (ident, kind, record["phone"], json.dumps(record, ensure_ascii=False)))

    def put_contact(self, record_id, value):
        record = _contact(value, record_id)
        with self._transaction() as connection:
            self._save_contact(connection, record, expected_revision=_revision(value))
        return record

    @staticmethod
    def _save_agent(connection, record, fingerprint, *, expected_revision=None):
        if connection.execute("SELECT 1 FROM agents WHERE fingerprint=? AND id!=?", (fingerprint, record["id"])).fetchone():
            raise WorkspaceConflict("An identical agent draft already exists.")
        exists = connection.execute("SELECT payload FROM agents WHERE id=?", (record["id"],)).fetchone()
        current_revision = _saved_revision(exists[0]) if exists else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise WorkspaceConflict("This agent changed in another browser. Your draft is unchanged. Close and reopen the editor to review the latest details before saving.")
        record["revision"] = current_revision + 1
        if not exists and connection.execute("SELECT count(*) FROM agents").fetchone()[0] >= MAX_AGENTS:
            raise WorkspaceConflict("The workspace agent limit has been reached.")
        connection.execute("INSERT INTO agents(id,fingerprint,payload) VALUES(?,?,?) "
                           "ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint,payload=excluded.payload",
                           (record["id"], fingerprint, json.dumps(record, ensure_ascii=False)))

    def put_agent(self, record_id, value):
        record, fingerprint = _agent(value, record_id)
        with self._transaction() as connection:
            self._save_agent(connection, record, fingerprint, expected_revision=_revision(value))
        return record

    def import_records(self, value):
        if not isinstance(value, dict) or set(value) != {"contacts", "demoOverrides", "agents"}:
            raise WorkspaceError("Import contacts, demoOverrides, and agents arrays.")
        for field, limit in (("contacts", MAX_CONTACTS), ("demoOverrides", len(DEMO_PHONES)), ("agents", MAX_AGENTS)):
            if not isinstance(value[field], list) or len(value[field]) > limit:
                raise WorkspaceError(f"Import allows at most {limit} {field}.")
        contacts = [_contact(record) for record in value["contacts"]]
        demos = [_contact(record) for record in value["demoOverrides"]]
        agents = [_agent(record, importing=True) for record in value["agents"]]
        if any(record["demo"] for record in contacts) or any(not record["demo"] for record in demos):
            raise WorkspaceError("Separate contacts from demoOverrides in the import.")
        with self._transaction() as connection:
            for record in demos + contacts:
                if (connection.execute("SELECT 1 FROM contacts WHERE id=?", (record["id"],)).fetchone()
                        or self._phone_exists(connection, record["phone"], record["id"])):
                    continue
                self._save_contact(connection, record)
            for record, fingerprint in agents:
                if connection.execute("SELECT 1 FROM agents WHERE id=? OR fingerprint=?", (record["id"], fingerprint)).fetchone():
                    continue
                self._save_agent(connection, record, fingerprint)
            return self._snapshot(connection)
