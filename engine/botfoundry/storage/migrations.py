"""Ordered, append-only schema migrations.

Rules: never edit a migration that has shipped; add a new one. Each migration runs in a
single transaction together with its bookkeeping row, so a failure leaves the database unchanged."""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .database import Database, StorageError


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial schema", (
        """CREATE TABLE workflows (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            definition  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )""",
        """CREATE TABLE runs (
            run_id             TEXT PRIMARY KEY,
            workflow_id        TEXT NOT NULL,
            workflow_name      TEXT NOT NULL,
            workflow_snapshot  TEXT NOT NULL,
            status             TEXT NOT NULL,
            error              TEXT,
            rulebook_version   INTEGER,
            created_at         TEXT NOT NULL,
            finished_at        TEXT
        )""",
        "CREATE INDEX idx_runs_created ON runs(created_at)",
        "CREATE INDEX idx_runs_workflow ON runs(workflow_id, created_at)",
        """CREATE TABLE node_runs (
            run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            node_id      TEXT NOT NULL,
            node_type    TEXT NOT NULL,
            status       TEXT NOT NULL,
            outputs      TEXT,
            summary      TEXT,
            error        TEXT,
            reason       TEXT,
            started_at   TEXT,
            finished_at  TEXT,
            PRIMARY KEY (run_id, node_id)
        )""",
        """CREATE TABLE run_events (
            run_id     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            id         INTEGER NOT NULL,
            type       TEXT NOT NULL,
            timestamp  TEXT NOT NULL,
            node_id    TEXT,
            payload    TEXT NOT NULL,
            PRIMARY KEY (run_id, id)
        )""",
    )),
)


def _check(migrations: tuple[Migration, ...]) -> None:
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError(f"Migration versions must be 1, 2, 3... in order; got {versions}")


def applied_versions(db: Database) -> set[int]:
    exists = db.query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'")
    if not exists:
        return set()
    return {row["version"] for row in db.query("SELECT version FROM schema_migrations")}


def migrate(db: Database, migrations: tuple[Migration, ...] = MIGRATIONS) -> int:
    """Bring the database up to date. Returns the schema version. Safe to call repeatedly."""
    _check(migrations)
    latest = migrations[-1].version if migrations else 0
    try:
        with db.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)""")
        applied = applied_versions(db)
        if applied and max(applied) > latest:
            raise StorageError(
                f"This database is schema version {max(applied)}, but this version of BotFoundry only "
                f"understands up to {latest}. Update BotFoundry; do not delete the database.")
        for migration in migrations:
            if migration.version in applied:
                continue
            try:
                with db.transaction() as conn:
                    for statement in migration.statements:
                        conn.execute(statement)
                    conn.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                                 (migration.version, migration.name, datetime.now(timezone.utc).isoformat()))
            except sqlite3.Error as exc:
                raise StorageError(f"Migration {migration.version} ('{migration.name}') failed and was "
                                   f"rolled back: {exc}") from exc
    except sqlite3.Error as exc:
        raise StorageError(f"Database error while migrating {db.path}: {exc}") from exc
    return latest
