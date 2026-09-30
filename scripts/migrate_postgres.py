"""Inspect or migrate both Phoney SQLite stores into an empty PG workspace.

Dry-run is the default and does not create PostgreSQL schemas or tables. Stop
application writers before the final --apply; retain the source SQLite files
and the separately stored call archive for rollback. Neither source is changed.
Supply DATABASE_URL in the environment or a file containing only the URL with
--database-url-file; database credentials and record payloads are never printed.
"""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from postgres_store import PostgresUnavailable, SCHEMA_VERSION, TABLES, schema_name, transaction


SOURCES = {
    "workspace.sqlite3": {
        "contacts": ("id", "kind", "phone", "payload"),
        "agents": ("id", "fingerprint", "payload"),
        "call_notifications": ("id", "payload", "unread"),
        "notification_state": ("id",),
    },
    "agent-execution.sqlite3": {
        "voices": ("id", "payload"),
        "revisions": ("agent_id", "revision", "payload"),
        "current_agents": ("id", "revision", "slot"),
        "grants": ("digest", "expires"),
        "owner_sessions": ("digest", "expires"),
        "clone_receipts": ("key", "payload"),
        "agent_setup": ("key", "value"),
    },
}
ORDERED = frozenset({"contacts", "agents"})


class MigrationError(Exception):
    pass


def read_source(storage_dir):
    """Read consistent SQLite snapshots through read-only connections."""
    folder = Path(storage_dir)
    snapshots = {}
    for filename, tables in SOURCES.items():
        path = folder / filename
        if not path.is_file() or path.is_symlink():
            raise MigrationError("Both regular SQLite database files must exist in the source directory.")
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                source.execute("PRAGMA query_only=ON")
                source.execute("PRAGMA trusted_schema=OFF")
                source.execute("BEGIN")
                if source.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                    raise MigrationError("The source SQLite schema version is unsupported.")
                if source.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise MigrationError("The source SQLite integrity check failed.")
                existing = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if existing - set(tables) - {"sqlite_sequence"}:
                    raise MigrationError("The source contains unknown tables; review them before migrating.")
                for table, columns in tables.items():
                    selected = columns + (("insertion_order",) if table in ORDERED else ())
                    if table not in existing:
                        snapshots[table] = {"columns": selected, "rows": []}
                        continue
                    actual = tuple(row[1] for row in source.execute(f"PRAGMA table_info({table})"))
                    if actual != columns:
                        raise MigrationError("The source SQLite columns are unsupported.")
                    fields = ",".join(columns) + (",rowid" if table in ORDERED else "")
                    ordering = "rowid" if table in ORDERED else ",".join(columns)
                    rows = source.execute(f"SELECT {fields} FROM {table} ORDER BY {ordering}").fetchall()
                    snapshots[table] = {"columns": selected, "rows": rows}
        except (OSError, sqlite3.Error):
            raise MigrationError("The source SQLite databases could not be read.") from None
    return snapshots


def target_state(connection, workspace_id):
    """Inspect a namespace without creating it; refuse unrelated tables."""
    schema = schema_name(workspace_id)
    exists = connection.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=?)", (schema,)).fetchone()[0]
    tables = {row[0] for row in connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=? AND table_type='BASE TABLE'", (schema,))}
    if tables - set(TABLES) - {"phoney_storage_meta"}:
        raise MigrationError("The target namespace contains unrelated tables.")
    if "phoney_storage_meta" in tables:
        version = connection.execute("SELECT version FROM phoney_storage_meta WHERE id=1").fetchone()
        if version is not None and version[0] != SCHEMA_VERSION:
            raise MigrationError("The target PostgreSQL schema version is unsupported.")
    counts = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
              if table in tables else 0 for table in TABLES}
    return {"schema_exists": exists, "empty": not any(counts.values()), "counts": counts}


def migrate(storage_dir, database_url, workspace_id="default", *, apply=False):
    snapshots = read_source(storage_dir)
    # Use read-only transactions for inspection, including an explicit server
    # guarantee against accidentally introducing DDL in the default mode.
    with transaction(database_url, workspace_id, initialize=False, read_only=True) as connection:
        before = target_state(connection, workspace_id)
    if not before["empty"]:
        raise MigrationError("The target workspace already contains records; nothing was copied.")
    counts = {table: len(snapshot["rows"]) for table, snapshot in snapshots.items()}
    if not apply:
        return {"mode": "dry-run", "workspace_id": workspace_id,
                "source_counts": counts, "target_schema_exists": before["schema_exists"], "target_empty": True}

    with transaction(database_url, workspace_id) as connection:
        # Recheck after acquiring the workspace transaction lock: another
        # process may have written since the read-only inspection above.
        if not target_state(connection, workspace_id)["empty"]:
            raise MigrationError("The target workspace acquired records; nothing was copied.")
        for table, snapshot in snapshots.items():
            columns, rows = snapshot["columns"], snapshot["rows"]
            if rows:
                marks = ",".join("?" for _ in columns)
                connection.executemany(f"INSERT INTO {table}({','.join(columns)}) VALUES({marks})", rows)
            if table in ORDERED and rows:
                maximum = max(row[-1] for row in rows)
                connection.execute("SELECT setval(pg_get_serial_sequence(?, 'insertion_order'), ?, ?)",
                                   (table, max(maximum, 1), maximum >= 1))
            ordering = "insertion_order" if table in ORDERED else ",".join(columns)
            copied = connection.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY {ordering}").fetchall()
            if copied != rows:
                raise MigrationError("Copied PostgreSQL data differs from its SQLite source; migration rolled back.")
        after = target_state(connection, workspace_id)
        if after["counts"] != counts:
            raise MigrationError("Copied PostgreSQL counts differ from SQLite; migration rolled back.")
    return {"mode": "applied", "workspace_id": workspace_id, "verified_counts": counts,
            "sources_unchanged": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-dir", help="Directory containing both SQLite files")
    parser.add_argument("--workspace-id")
    parser.add_argument("--env-file", help="Private .env file containing database/storage/workspace configuration")
    parser.add_argument("--database-url-file", help="Private file containing the target database URL")
    parser.add_argument("--apply", action="store_true", help="Copy and verify records; otherwise inspect only")
    args = parser.parse_args(argv)
    try:
        configuration = {}
        if args.env_file:
            from dotenv import dotenv_values
            if not Path(args.env_file).is_file():
                raise MigrationError("The specified environment file is unavailable.")
            configuration = dotenv_values(args.env_file)
        database_url = Path(args.database_url_file).read_text().strip() if args.database_url_file else (
            configuration.get("DATABASE_URL") or os.getenv("DATABASE_URL", "")).strip()
        if not database_url:
            raise MigrationError("Set DATABASE_URL or provide --database-url-file.")
        storage_dir = args.storage_dir or configuration.get("WORKSPACE_STORAGE_DIR") or os.getenv("WORKSPACE_STORAGE_DIR", "")
        if not storage_dir:
            raise MigrationError("Set WORKSPACE_STORAGE_DIR or provide --storage-dir.")
        workspace_id = args.workspace_id or configuration.get("WORKSPACE_ID") or os.getenv("WORKSPACE_ID", "default")
        report = migrate(storage_dir, database_url, workspace_id, apply=args.apply)
    except (MigrationError, PostgresUnavailable) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print("Migration configuration is invalid or unavailable; nothing was copied.", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
