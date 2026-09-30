"""Persistent, shared-workspace credentials, trusted devices, and guess limits.

PostgreSQL is used when DATABASE_URL is configured. The SQLite backend keeps the
same transactional guarantees for local development and isolated tests.
"""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path


SESSION_SECONDS = 30 * 24 * 60 * 60
SHORT_SESSION_SECONDS = 12 * 60 * 60
PIN_WINDOW_SECONDS = 15 * 60
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
DEVICE = re.compile(r"([A-Za-z0-9_-]{43})\.([0-9a-f]{64})\Z")
SQLITE_LOCK = threading.RLock()


class AccessUnavailable(Exception):
    """Storage errors intentionally contain no connection strings or secrets."""


class AccessNotConfigured(Exception):
    pass


def hash_credential(value):
    """OWASP's 32-MiB scrypt tradeoff; salts prevent shared PIN hash reuse."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value.encode("utf-8"), salt=salt, n=2**15, r=8,
                            p=3, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt-v1:" + salt.hex() + ":" + digest.hex()


def verify_credential(value, encoded):
    try:
        version, salt, expected = encoded.split(":")
        if version != "scrypt-v1" or len(salt) != 32 or len(expected) != 64:
            return False
        digest = hashlib.scrypt(value.encode("utf-8"), salt=bytes.fromhex(salt),
                                n=2**15, r=8, p=3, maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(digest, bytes.fromhex(expected))
    except (AttributeError, TypeError, ValueError, UnicodeError):
        return False


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class WorkspaceAccess:
    def __init__(self, storage_dir="", database_url="", workspace_id="default", *, clock=time.time):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", workspace_id):
            raise ValueError("WORKSPACE_ID must be a short workspace identifier.")
        self.database_url = database_url
        self.workspace_id = workspace_id
        self.clock = clock
        self.path = None
        if not database_url:
            if not storage_dir:
                raise AccessUnavailable("Workspace access storage is unavailable.")
            self.path = Path(storage_dir) / "workspace_access.sqlite3"
        with self._transaction() as db:
            for sql in (
                """CREATE TABLE IF NOT EXISTS workspace_access_credentials (
                    workspace_id TEXT PRIMARY KEY, pin_hash TEXT NOT NULL,
                    password_hash TEXT NOT NULL, device_secret TEXT NOT NULL,
                    version BIGINT NOT NULL, updated_at BIGINT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS workspace_access_sessions (
                    workspace_id TEXT NOT NULL, token_hash TEXT NOT NULL,
                    credential_version BIGINT NOT NULL, created_at BIGINT NOT NULL,
                    expires_at BIGINT NOT NULL, PRIMARY KEY(workspace_id, token_hash))""",
                """CREATE TABLE IF NOT EXISTS workspace_access_guards (
                    workspace_id TEXT NOT NULL, guard_key TEXT NOT NULL,
                    failures BIGINT NOT NULL, window_started BIGINT NOT NULL,
                    PRIMARY KEY(workspace_id, guard_key))""",
            ):
                db.execute(sql)

    @contextmanager
    def _transaction(self):
        try:
            if self.database_url:
                from psycopg.rows import dict_row
                from postgres_store import transaction
                # All auth rows share one namespace. The adapter's transaction
                # advisory lock serializes guesses even across server workers.
                with transaction(self.database_url, "__auth__", row_factory=dict_row, storage_tables=False) as db:
                    yield db
            else:
                from workspace_store import _directory
                with SQLITE_LOCK:
                    root = _directory(self.path.parent)
                    db = None
                    try:
                        for suffix in ("", "-journal", "-wal", "-shm"):
                            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                            try:
                                if not suffix:
                                    try:
                                        fd = os.open(self.path.name, flags | os.O_CREAT | os.O_EXCL,
                                                     0o600, dir_fd=root)
                                    except FileExistsError:
                                        fd = os.open(self.path.name, flags, dir_fd=root)
                                else:
                                    fd = os.open(self.path.name + suffix, flags, dir_fd=root)
                            except FileNotFoundError:
                                continue
                            try:
                                info = os.fstat(fd)
                                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                                    raise AccessUnavailable("Workspace access storage is unavailable.")
                                os.fchmod(fd, 0o600)
                            finally:
                                os.close(fd)
                        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
                        db.row_factory = sqlite3.Row
                        db.execute("PRAGMA trusted_schema=OFF")
                        db.execute("PRAGMA synchronous=FULL")
                        db.execute("BEGIN IMMEDIATE")
                        yield db
                        db.commit()
                    except BaseException:
                        if db:
                            db.rollback()
                        raise
                    finally:
                        if db:
                            db.close()
                        os.close(root)
        except (AccessNotConfigured, AccessUnavailable):
            raise
        except Exception:
            raise AccessUnavailable("Workspace access storage is unavailable.") from None

    def _credentials(self, db):
        row = db.execute("SELECT * FROM workspace_access_credentials WHERE workspace_id=?",
                         (self.workspace_id,)).fetchone()
        if row is None:
            raise AccessNotConfigured("Configure a workspace PIN and recovery password locally.")
        return row

    def configure(self, pin, password):
        if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{6}", pin):
            raise ValueError("The workspace PIN must contain exactly six digits.")
        if (not isinstance(password, str) or len(password) < 15
                or len(password.encode("utf-8")) > 1024):
            raise ValueError("The recovery password must contain at least 15 characters (maximum 1024 bytes).")
        pin_hash, password_hash = hash_credential(pin), hash_credential(password)
        now = int(self.clock())
        with self._transaction() as db:
            old = db.execute("SELECT version FROM workspace_access_credentials WHERE workspace_id=?",
                             (self.workspace_id,)).fetchone()
            version = old["version"] + 1 if old else 1
            db.execute("""INSERT INTO workspace_access_credentials
                (workspace_id,pin_hash,password_hash,device_secret,version,updated_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET
                pin_hash=excluded.pin_hash,password_hash=excluded.password_hash,
                device_secret=excluded.device_secret,version=excluded.version,
                updated_at=excluded.updated_at""",
                (self.workspace_id, pin_hash, password_hash, secrets.token_hex(32), version, now))
            db.execute("DELETE FROM workspace_access_sessions WHERE workspace_id=?", (self.workspace_id,))
            db.execute("DELETE FROM workspace_access_guards WHERE workspace_id=?", (self.workspace_id,))

    def _device(self, value, credentials):
        secret = bytes.fromhex(credentials["device_secret"])
        match = DEVICE.fullmatch(value or "")
        if match:
            expected = hmac.new(secret, match[1].encode("ascii"), hashlib.sha256).hexdigest()
            if hmac.compare_digest(match[2], expected):
                return value
        token = secrets.token_urlsafe(32)
        return token + "." + hmac.new(secret, token.encode("ascii"), hashlib.sha256).hexdigest()

    def _keys(self, device, ip, credentials):
        address = hmac.new(bytes.fromhex(credentials["device_secret"]),
                           str(ip).encode("utf-8"), hashlib.sha256).hexdigest()
        return "device:" + _digest(device), "ip:" + address

    def _count(self, db, key, now, window=None):
        row = db.execute("SELECT failures,window_started FROM workspace_access_guards "
                         "WHERE workspace_id=? AND guard_key=?", (self.workspace_id, key)).fetchone()
        if row is None or (window is not None and now >= row["window_started"] + window):
            return 0, now
        return row["failures"], row["window_started"]

    def _increment(self, db, key, now, window=None):
        count, started = self._count(db, key, now, window)
        db.execute("""INSERT INTO workspace_access_guards
            (workspace_id,guard_key,failures,window_started) VALUES (?,?,?,?)
            ON CONFLICT(workspace_id,guard_key) DO UPDATE SET
            failures=excluded.failures,window_started=excluded.window_started""",
            (self.workspace_id, key, count + 1, started))
        return count + 1

    def _pin_state(self, db, device_key, ip_key, now):
        device, _ = self._count(db, "pin:" + device_key, now)
        address, _ = self._count(db, "pin:" + ip_key, now, PIN_WINDOW_SECONDS)
        workspace, started = self._count(db, "pin:workspace", now, PIN_WINDOW_SECONDS)
        result = {"password_required": device >= 3 or address >= 3 or workspace >= 15,
                  "attempts_remaining": max(0, 3 - max(device, address))}
        if workspace >= 15:
            result.update(attempts_remaining=0, retry_after=max(1, started + PIN_WINDOW_SECONDS - now),
                          throttle_scope="pin")
        return result

    def _authenticated(self, db, token, credentials, now):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return False
        row = db.execute("SELECT expires_at,credential_version FROM workspace_access_sessions "
                         "WHERE workspace_id=? AND token_hash=?",
                         (self.workspace_id, _digest(token))).fetchone()
        return bool(row and row["expires_at"] > now and row["credential_version"] == credentials["version"])

    def authenticated(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return False
        with self._transaction() as db:
            return self._authenticated(db, token, self._credentials(db), int(self.clock()))

    def status(self, device, ip, token=None):
        now = int(self.clock())
        with self._transaction() as db:
            credentials = self._credentials(db)
            db.execute("DELETE FROM workspace_access_guards WHERE workspace_id=? AND window_started<?",
                       (self.workspace_id, now - SESSION_SECONDS))
            device = self._device(device, credentials)
            keys = self._keys(device, ip, credentials)
            result = {"enabled": True, "configured": True,
                      "authenticated": self._authenticated(db, token, credentials, now),
                      **self._pin_state(db, *keys, now)}
            return result, device

    def unlock(self, device, ip, method, credential, remember=True):
        now = int(self.clock())
        with self._transaction() as db:
            credentials = self._credentials(db)
            db.execute("DELETE FROM workspace_access_guards WHERE workspace_id=? AND window_started<?",
                       (self.workspace_id, now - SESSION_SECONDS))
            device = self._device(device, credentials)
            device_key, ip_key = self._keys(device, ip, credentials)
            pin_state = self._pin_state(db, device_key, ip_key, now)
            # Cheap checks occur before scrypt. Refreshing or replacing a device
            # cookie does not reset source-IP or workspace-wide counters.
            request_guards = [("request:" + device_key, 20), ("request:" + ip_key, 30),
                              ("request:workspace", 120)]
            for key, limit in request_guards:
                count, started = self._count(db, key, now, 60)
                if count >= limit:
                    return 429, {"error": "throttled", "message": "Wait before trying again.",
                                 **pin_state, "retry_after": max(1, started + 60 - now),
                                 "throttle_scope": "request"}, None, device
            for key, _ in request_guards:
                self._increment(db, key, now, 60)
            if method == "pin" and pin_state["password_required"]:
                error = "throttled" if pin_state.get("throttle_scope") else "password_required"
                return (429 if error == "throttled" else 403), {
                    "error": error, "message": "Use the recovery password to unlock.",
                    **pin_state}, None, device
            if method == "password":
                for key, limit in (("password:" + ip_key, 10), ("password:workspace", 40)):
                    count, started = self._count(db, key, now, 15 * 60)
                    if count >= limit:
                        return 429, {"error": "throttled", "message": "Wait before trying the password again.",
                                     **pin_state, "retry_after": max(1, started + 15 * 60 - now),
                                     "throttle_scope": "request"}, None, device
            valid_shape = (bool(re.fullmatch(r"[0-9]{6}", credential)) if method == "pin"
                           else 0 < len(credential.encode("utf-8")) <= 1024)
            if not valid_shape or not verify_credential(credential, credentials[method + "_hash"]):
                if method == "pin":
                    self._increment(db, "pin:" + device_key, now)
                    self._increment(db, "pin:" + ip_key, now, PIN_WINDOW_SECONDS)
                    self._increment(db, "pin:workspace", now, PIN_WINDOW_SECONDS)
                    pin_state = self._pin_state(db, device_key, ip_key, now)
                    required = pin_state["password_required"]
                    return (403 if required else 401), {
                        "error": "password_required" if required else "pin_invalid",
                        "message": "Use the recovery password to unlock." if required else "Incorrect PIN.",
                        **pin_state}, None, device
                self._increment(db, "password:" + ip_key, now, 15 * 60)
                self._increment(db, "password:workspace", now, 15 * 60)
                return 401, {"error": "password_invalid", "message": "Incorrect recovery password.",
                             **pin_state}, None, device
            token = secrets.token_urlsafe(32)
            seconds = SESSION_SECONDS if remember else SHORT_SESSION_SECONDS
            db.execute("INSERT INTO workspace_access_sessions "
                       "(workspace_id,token_hash,credential_version,created_at,expires_at) VALUES (?,?,?,?,?)",
                       (self.workspace_id, _digest(token), credentials["version"], now, now + seconds))
            db.execute("DELETE FROM workspace_access_sessions WHERE workspace_id=? AND expires_at<=?",
                       (self.workspace_id, now))
            oldest = db.execute("SELECT token_hash FROM workspace_access_sessions WHERE workspace_id=? "
                                "ORDER BY CASE WHEN token_hash=? THEN 0 ELSE 1 END,created_at DESC,token_hash DESC "
                                "LIMIT 1024 OFFSET 64", (self.workspace_id, _digest(token))).fetchall()
            for row in oldest:
                db.execute("DELETE FROM workspace_access_sessions WHERE workspace_id=? AND token_hash=?",
                           (self.workspace_id, row["token_hash"]))
            for key in ("pin:" + device_key, "pin:" + ip_key, "password:" + ip_key):
                db.execute("DELETE FROM workspace_access_guards WHERE workspace_id=? AND guard_key=?",
                           (self.workspace_id, key))
            return 200, {"authenticated": True, "redirect": "/dashboard"}, token, device

    def revoke(self, token=None):
        with self._transaction() as db:
            if token is None:
                db.execute("DELETE FROM workspace_access_sessions WHERE workspace_id=?", (self.workspace_id,))
            elif isinstance(token, str) and TOKEN.fullmatch(token):
                db.execute("DELETE FROM workspace_access_sessions WHERE workspace_id=? AND token_hash=?",
                           (self.workspace_id, _digest(token)))
