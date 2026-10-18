"""A small, thread-safe wrapper around one SQLite connection.

One connection plus a lock is plenty for a single-user local app and keeps behavior simple:
every write happens inside an explicit transaction, and either fully succeeds or leaves no trace."""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


class StorageError(Exception):
    """A database problem the user can be told about (cannot open, corrupt, too new, migration failed)."""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None: we control transactions explicitly (see transaction()).
            conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()  # fails on a non-database file
            return conn
        except (sqlite3.DatabaseError, OSError) as exc:
            if conn is not None:
                conn.close()
            raise StorageError(f"Cannot open the database at {self.path}: {exc}") from exc

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """All-or-nothing group of statements. Not re-entrant."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
