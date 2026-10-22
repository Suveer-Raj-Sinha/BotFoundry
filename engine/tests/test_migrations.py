import sqlite3

import pytest

from botfoundry.storage.database import Database, StorageError
from botfoundry.storage.migrations import MIGRATIONS, Migration, applied_versions, migrate


NEXT = len(MIGRATIONS) + 1          # the first version number that does not exist yet


def tables(db):
    return {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_gets_the_full_schema():
    db = Database(":memory:")
    assert migrate(db) == MIGRATIONS[-1].version
    assert {"workflows", "runs", "node_runs", "run_events", "schema_migrations"} <= tables(db)
    assert applied_versions(db) == {m.version for m in MIGRATIONS}


def test_running_migrations_again_changes_nothing():
    db = Database(":memory:")
    migrate(db)
    db.query("SELECT 1")
    with db.transaction() as conn:
        conn.execute("INSERT INTO workflows VALUES ('w', 'W', '{}', 'now', 'now')")
    migrate(db)
    migrate(db)
    assert db.query_one("SELECT COUNT(*) AS n FROM workflows")["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM schema_migrations")["n"] == len(MIGRATIONS)


def test_upgrade_path_keeps_existing_data(tmp_path):
    path = tmp_path / "upgrade.db"
    first = Database(path)
    migrate(first)
    with first.transaction() as conn:
        conn.execute("INSERT INTO workflows VALUES ('w', 'Saved', '{}', 'then', 'then')")
    first.close()

    extra = Migration(NEXT, "add notes", ("ALTER TABLE workflows ADD COLUMN notes TEXT DEFAULT ''",))
    second = Database(path)
    assert migrate(second, MIGRATIONS + (extra,)) == NEXT
    row = second.query_one("SELECT name, notes FROM workflows WHERE id='w'")
    assert (row["name"], row["notes"]) == ("Saved", "")
    assert applied_versions(second) == set(range(1, NEXT + 1))


def test_failed_migration_rolls_back_completely():
    db = Database(":memory:")
    migrate(db)
    broken = Migration(NEXT, "half broken", (
        "CREATE TABLE should_not_survive (x INTEGER)",
        "THIS IS NOT SQL",
    ))
    with pytest.raises(StorageError, match="rolled back"):
        migrate(db, MIGRATIONS + (broken,))
    assert "should_not_survive" not in tables(db)      # first statement was undone
    assert applied_versions(db) == {m.version for m in MIGRATIONS}      # and the new version was never recorded


def test_database_from_a_newer_version_is_refused():
    db = Database(":memory:")
    migrate(db, MIGRATIONS + (Migration(NEXT, "future", ("CREATE TABLE future_stuff (x INTEGER)",)),))
    with pytest.raises(StorageError, match="Update BotFoundry"):
        migrate(db)                                     # code only knows version 1
    assert "future_stuff" in tables(db)                 # and nothing was touched


@pytest.mark.parametrize("bad", [
    (Migration(2, "skips one", ()),),
    (Migration(1, "a", ()), Migration(1, "dup", ())),
    (Migration(1, "a", ()), Migration(3, "gap", ())),
])
def test_migration_list_must_be_consecutive(bad):
    with pytest.raises(ValueError):
        migrate(Database(":memory:"), bad)


def test_corrupt_file_gives_a_clear_error(tmp_path):
    path = tmp_path / "broken.db"
    path.write_bytes(b"this is definitely not a sqlite database " * 50)
    with pytest.raises(StorageError, match="Cannot open the database"):
        Database(path)


def test_creates_missing_folders(tmp_path):
    path = tmp_path / "a" / "b" / "c" / "x.db"
    db = Database(path)
    migrate(db)
    assert path.exists()


def test_foreign_keys_are_enforced():
    db = Database(":memory:")
    migrate(db)
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO node_runs (run_id, node_id, node_type, status) "
                         "VALUES ('missing', 'n', 't', 'pending')")
