"""Private execution configuration, deliberately separate from public drafts.

Publishing copies owner-approved text into an immutable revision. Neither a
browser draft edit nor a later publication changes the snapshot held by a call.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time

from workspace_store import _directory, WorkspaceError
from voice_stack.settings import VOICE_ID

DATABASE_NAME = "agent-execution.sqlite3"
AGENT_ID = re.compile(r"agent-[A-Za-z0-9-]{8,80}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
SESSION_SECONDS = 12 * 60 * 60
MAX_AGENTS = 50
MAX_VOICES = 500
LOCK = threading.RLock()
DEFAULT_AGENT_ID = "agent-voice-clone-default"
DEFAULT_PERSONALITY = "Tries to hang the call up asap"


class RegistryError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status_code = status


def text(value, name, limit, *, empty=False, multiline=False):
    if not isinstance(value, str) or len(value) > limit:
        raise RegistryError(f"Use at most {limit} characters for {name}.")
    value = value.strip()
    if (not value and not empty) or any(ord(c) < 32 and c not in ("\n\t" if multiline else "") for c in value):
        raise RegistryError(f"Enter a valid {name}.")
    return value


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class AgentSnapshot:
    id: str
    name: str
    prompt: str
    revision: int
    voice_profile_id: str | None
    voice_id: str
    slot: int | None

    def to_dict(self):
        return {"id": self.id, "name": self.name, "prompt": self.prompt,
                "revision": self.revision, "voiceProfileId": self.voice_profile_id,
                "voiceId": self.voice_id, "slot": self.slot}

    @classmethod
    def from_dict(cls, value):
        return cls(value["id"], value["name"], value["prompt"], value["revision"],
                   value["voiceProfileId"], value["voiceId"], value["slot"])


class AgentRegistry:
    def __init__(self, storage_dir, *, clock=time.time):
        self.path = Path(storage_dir) if storage_dir else None
        self.enabled = bool(storage_dir)
        self.clock = clock

    @contextmanager
    def _transaction(self):
        if self.path is None:
            raise RegistryError("Agent storage is not configured.", 503)
        connection = root = None
        with LOCK:
            try:
                root = _directory(self.path)
                for suffix in ("", "-journal", "-wal", "-shm"):
                    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                    try:
                        if not suffix:
                            try:
                                fd = os.open(DATABASE_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root)
                            except FileExistsError:
                                fd = os.open(DATABASE_NAME, flags, dir_fd=root)
                        else:
                            fd = os.open(DATABASE_NAME + suffix, flags, dir_fd=root)
                    except FileNotFoundError:
                        continue
                    try:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                            raise RegistryError("Agent storage is unavailable.", 503)
                        os.fchmod(fd, 0o600)
                    finally:
                        os.close(fd)
                connection = sqlite3.connect(str(self.path / DATABASE_NAME), timeout=5, isolation_level=None)
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                    raise RegistryError("Agent storage version is unsupported.", 503)
                connection.execute("CREATE TABLE IF NOT EXISTS voices (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS revisions (agent_id TEXT NOT NULL, revision INTEGER NOT NULL, "
                                   "payload TEXT NOT NULL, PRIMARY KEY(agent_id,revision))")
                connection.execute("CREATE TABLE IF NOT EXISTS current_agents (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
                                   "slot INTEGER UNIQUE CHECK(slot IS NULL OR slot BETWEEN 1 AND 9))")
                connection.execute("CREATE TABLE IF NOT EXISTS grants (digest TEXT PRIMARY KEY, expires REAL NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS owner_sessions (digest TEXT PRIMARY KEY, expires REAL NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS clone_receipts (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS agent_setup (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("PRAGMA user_version=1")
                yield connection
                connection.commit()
            except RegistryError:
                raise
            except (WorkspaceError, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
                raise RegistryError("Agent storage is unavailable. Changes were not saved.", 503) from None
            finally:
                if connection is not None:
                    connection.close()
                if root is not None:
                    os.close(root)

    def grant(self, *, ttl=300):
        """Called locally only. The returned secret is never stored in plaintext."""
        if type(ttl) is not int or not 30 <= ttl <= 600:
            raise RegistryError("Owner grants expire after 30–600 seconds.")
        code = secrets.token_urlsafe(32)
        with self._transaction() as db:
            db.execute("DELETE FROM grants WHERE expires<=?", (self.clock(),))
            db.execute("DELETE FROM grants WHERE digest IN (SELECT digest FROM grants ORDER BY expires DESC LIMIT -1 OFFSET 15)")
            db.execute("INSERT INTO grants VALUES(?,?)", (_digest(code), self.clock() + ttl))
        return code

    def consume_grant(self, code):
        if not isinstance(code, str) or not TOKEN.fullmatch(code):
            raise RegistryError("The owner access code is invalid or expired.", 403)
        token = secrets.token_urlsafe(32)
        with self._transaction() as db:
            found = db.execute("DELETE FROM grants WHERE digest=? AND expires>? RETURNING digest",
                               (_digest(code), self.clock())).fetchone()
            if not found:
                raise RegistryError("The owner access code is invalid or expired.", 403)
            db.execute("DELETE FROM owner_sessions WHERE expires<=?", (self.clock(),))
            db.execute("DELETE FROM owner_sessions WHERE digest IN (SELECT digest FROM owner_sessions ORDER BY expires DESC LIMIT -1 OFFSET 31)")
            db.execute("INSERT INTO owner_sessions VALUES(?,?)", (_digest(token), self.clock() + SESSION_SECONDS))
        return token

    def authorized(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token) or not self.enabled:
            return False
        with self._transaction() as db:
            return db.execute("SELECT 1 FROM owner_sessions WHERE digest=? AND expires>?",
                              (_digest(token), self.clock())).fetchone() is not None

    def revoke(self, token):
        if isinstance(token, str) and TOKEN.fullmatch(token):
            with self._transaction() as db:
                db.execute("DELETE FROM owner_sessions WHERE digest=?", (_digest(token),))

    def revoke_all(self):
        with self._transaction() as db:
            db.execute("DELETE FROM owner_sessions")
            db.execute("DELETE FROM grants")

    @staticmethod
    def _voices(db):
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM voices ORDER BY id LIMIT ?", (MAX_VOICES,))]

    def snapshot(self):
        with self._transaction() as db:
            agents = []
            for raw, slot in db.execute("SELECT r.payload,c.slot FROM current_agents c JOIN revisions r "
                                       "ON r.agent_id=c.id AND r.revision=c.revision ORDER BY c.id LIMIT ?", (MAX_AGENTS,)):
                value = json.loads(raw)
                value["slot"] = slot
                agents.append(value)
            return {"agents": agents, "voices": self._voices(db)}

    def replace_voices(self, records):
        """Commit a complete validated catalog; disappeared voices become unusable."""
        if not isinstance(records, list) or len(records) > MAX_VOICES:
            raise RegistryError("The voice catalog is too large.", 502)
        normalized = [self._voice(item) for item in records]
        if len({voice["id"] for voice in normalized}) != len(normalized):
            raise RegistryError("The voice catalog contained duplicate voices.", 502)
        with self._transaction() as db:
            existing = self._voices(db)
            incoming = {voice["id"] for voice in normalized}
            for item in existing:
                if item["id"] not in incoming:
                    item["ready"] = False
                    item["available"] = False
                    db.execute("UPDATE voices SET payload=? WHERE id=?", (json.dumps(item), item["id"]))
            for item in normalized:
                db.execute("INSERT INTO voices VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                           (item["id"], json.dumps(item)))
            if db.execute("SELECT count(*) FROM voices").fetchone()[0] > MAX_VOICES:
                raise RegistryError("The workspace voice limit has been reached.", 409)
            return self._voices(db)

    @staticmethod
    def _voice(value):
        voice_id = value.get("voiceId") if isinstance(value, dict) else None
        if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id):
            raise RegistryError("The provider returned an invalid voice identifier.", 502)
        name = text(value.get("name"), "voice name", 80)
        if type(value.get("ready")) is not bool or type(value.get("requiresVerification")) is not bool:
            raise RegistryError("The provider returned an invalid voice status.", 502)
        return {"id": "voice-" + voice_id, "voiceId": voice_id, "name": name,
                "ready": value["ready"] and not value["requiresVerification"],
                "requiresVerification": value["requiresVerification"], "available": True}

    def add_voice(self, value, *, consent=False):
        voice = self._voice(value)
        with self._transaction() as db:
            if not db.execute("SELECT 1 FROM voices WHERE id=?", (voice["id"],)).fetchone() and db.execute("SELECT count(*) FROM voices").fetchone()[0] >= MAX_VOICES:
                raise RegistryError("The workspace voice limit has been reached.", 409)
            db.execute("INSERT INTO voices VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (voice["id"], json.dumps(voice)))
            if consent:
                db.execute("INSERT OR REPLACE INTO clone_receipts VALUES(?,?)", (voice["id"], json.dumps({
                    "voiceId": voice["voiceId"], "consent": True, "at": self.clock()})))
        return voice

    @staticmethod
    def _save_revision(db, agent_id, name, prompt, voice_profile, voice_id, slot):
        current = db.execute("SELECT revision FROM current_agents WHERE id=?", (agent_id,)).fetchone()
        if current is None and db.execute("SELECT count(*) FROM current_agents").fetchone()[0] >= MAX_AGENTS:
            raise RegistryError("The workspace agent limit has been reached.", 409)
        revision = current[0] + 1 if current else 1
        snapshot = AgentSnapshot(agent_id, name, prompt, revision, voice_profile, voice_id, slot)
        db.execute("INSERT INTO revisions VALUES(?,?,?)", (agent_id, revision, json.dumps(snapshot.to_dict())))
        db.execute("INSERT INTO current_agents VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,slot=excluded.slot",
                   (agent_id, revision, slot))
        return snapshot

    def ensure_default_voice_clone(self):
        """Create the demo's first agent once, without resetting later edits.

        A matching existing #1 agent is adopted rather than duplicated. If #1
        belongs to different instructions, preserve that agent and its history
        in a new unassigned revision before assigning the requested default.
        No provider operation or call activation happens here.
        """
        with self._transaction() as db:
            marker = db.execute("SELECT value FROM agent_setup WHERE key='voice-clone-default'").fetchone()
            ident = marker[0] if marker else DEFAULT_AGENT_ID
            existing = db.execute("SELECT r.payload FROM current_agents c JOIN revisions r "
                                  "ON r.agent_id=c.id AND r.revision=c.revision WHERE c.id=?", (ident,)).fetchone()
            if marker or existing:
                if not marker:
                    db.execute("INSERT INTO agent_setup VALUES('voice-clone-default',?)", (ident,))
                return AgentSnapshot.from_dict(json.loads(existing[0])) if existing else None
            voice = next((item for item in self._voices(db)
                          if item["name"].casefold() == "owner" and item["ready"]), None)
            if voice is None:
                return None
            occupied = db.execute("SELECT r.payload FROM current_agents c JOIN revisions r "
                                  "ON r.agent_id=c.id AND r.revision=c.revision WHERE c.slot=1").fetchone()
            if occupied:
                other = AgentSnapshot.from_dict(json.loads(occupied[0]))
                if (other.voice_profile_id == voice["id"]
                        and other.prompt.rstrip(". ") == DEFAULT_PERSONALITY):
                    ident = other.id
                else:
                    self._save_revision(db, other.id, other.name, other.prompt,
                                        other.voice_profile_id, other.voice_id, None)
            saved = self._save_revision(db, ident, "Voice Clone", DEFAULT_PERSONALITY,
                                        voice["id"], voice["voiceId"], 1)
            db.execute("INSERT INTO agent_setup VALUES('voice-clone-default',?)", (ident,))
            return saved

    def publish(self, agent_id, value):
        if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
            raise RegistryError("Enter a valid agent identifier.")
        if not isinstance(value, dict) or set(value) != {"name", "prompt", "voiceProfileId", "slot"}:
            raise RegistryError("Enter name, prompt, voiceProfileId, and slot.")
        name = text(value["name"], "agent name", 80)
        prompt = text(value["prompt"], "agent prompt", 8000, empty=True, multiline=True)
        slot = value["slot"]
        if slot is not None and (type(slot) is not int or not 1 <= slot <= 9):
            raise RegistryError("Choose a keypad slot from 1 to 9, or leave it unassigned.")
        voice_profile = value["voiceProfileId"]
        if voice_profile is not None and (not isinstance(voice_profile, str) or not voice_profile.startswith("voice-")
                                          or not VOICE_ID.fullmatch(voice_profile[6:])):
            raise RegistryError("Choose a voice from this workspace.")
        with self._transaction() as db:
            voice_id = ""
            if voice_profile:
                row = db.execute("SELECT payload FROM voices WHERE id=?", (voice_profile,)).fetchone()
                if row is None:
                    raise RegistryError("Refresh the voice catalog and choose an available voice.", 409)
                voice = json.loads(row[0])
                voice_id = voice["voiceId"]
                if slot is not None and not voice["ready"]:
                    raise RegistryError("This voice is not ready. Complete provider verification before assigning a slot.", 409)
            if slot is not None and (not voice_id or not prompt):
                raise RegistryError("A keypad slot needs a ready voice and an agent prompt.", 409)
            if slot is not None and db.execute("SELECT 1 FROM current_agents WHERE slot=? AND id!=?", (slot, agent_id)).fetchone():
                raise RegistryError(f"Keypad #{slot} already belongs to another agent.", 409)
            snapshot = self._save_revision(db, agent_id, name, prompt, voice_profile, voice_id, slot)
        return snapshot

    def resolve_slot(self, key):
        if not isinstance(key, str) or key not in tuple("123456789") or not self.enabled:
            return None
        with self._transaction() as db:
            row = db.execute("SELECT r.payload FROM current_agents c JOIN revisions r ON r.agent_id=c.id "
                             "AND r.revision=c.revision WHERE c.slot=?", (int(key),)).fetchone()
            if row is None:
                return None
            value = json.loads(row[0])
            voice = db.execute("SELECT payload FROM voices WHERE id=?", (value["voiceProfileId"],)).fetchone()
            if voice is None or not json.loads(voice[0])["ready"]:
                return None
            return AgentSnapshot.from_dict(value)
