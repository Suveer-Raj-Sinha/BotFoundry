import pytest

from botfoundry.rules.models import Rulebook
from botfoundry.storage import Storage
from botfoundry.storage.database import Database
from botfoundry.storage.migrations import MIGRATIONS, migrate


def book(bot_id="fixer", **changes):
    data = {"bot_id": bot_id, "name": "Fixer", "allowed_folders": ["/data/in"],
            "permissions": {"fs.read": "allow", "fs.move": "ask"}}
    data.update(changes)
    return Rulebook.model_validate(data)


@pytest.fixture()
def storage():
    s = Storage.open(":memory:")
    yield s
    s.close()


def test_first_save_creates_version_one(storage):
    assert storage.save_rulebook(book(), "first") == (1, True)
    found = storage.get_rulebook("fixer")
    assert found["version"] == 1 and found["note"] == "first" and found["rulebook"] == book()


def test_identical_content_does_not_create_a_new_version(storage):
    storage.save_rulebook(book())
    reordered = Rulebook.model_validate({"permissions": {"fs.move": "ask", "fs.read": "allow"},
                                         "allowed_folders": ["/data/in"], "name": "Fixer", "bot_id": "fixer"})
    assert storage.save_rulebook(reordered, "no change") == (1, False)
    assert len(storage.list_rulebook_versions("fixer")) == 1


def test_a_change_creates_the_next_version_and_old_ones_stay_intact(storage):
    storage.save_rulebook(book())
    assert storage.save_rulebook(book(permissions={"fs.read": "allow"}), "tightened") == (2, True)
    assert storage.save_rulebook(book(name="Renamed")) == (3, True)
    assert storage.get_rulebook("fixer")["version"] == 3
    assert storage.get_rulebook("fixer", 1)["rulebook"] == book()                 # version 1 is exactly as saved
    assert storage.get_rulebook("fixer", 2)["note"] == "tightened"
    assert [v["version"] for v in storage.list_rulebook_versions("fixer")] == [3, 2, 1]
    assert storage.get_rulebook("fixer", 9) is None and storage.get_rulebook("nobody") is None


def test_going_back_to_old_content_is_a_new_version_not_an_edit(storage):
    storage.save_rulebook(book())
    storage.save_rulebook(book(name="Other"))
    assert storage.save_rulebook(book()) == (3, True)                              # history only ever grows


def test_bots_are_independent_and_listed_by_latest(storage):
    storage.save_rulebook(book("a"))
    storage.save_rulebook(book("a", name="A2"))
    storage.save_rulebook(book("b"))
    assert [(r["bot_id"], r["version"], r["name"]) for r in storage.list_rulebooks()] == [("a", 2, "A2"), ("b", 1, "Fixer")]


def test_rulebooks_survive_a_restart(tmp_path):
    first = Storage.open(tmp_path / "r.db")
    first.save_rulebook(book())
    first.close()
    second = Storage.open(tmp_path / "r.db")
    assert second.get_rulebook("fixer")["rulebook"] == book()
    second.close()


def test_upgrading_from_the_previous_schema_keeps_existing_data(tmp_path):
    path = tmp_path / "old.db"
    old = Database(path)
    migrate(old, MIGRATIONS[:1])                                  # a database created before Rulebooks existed
    with old.transaction() as conn:
        conn.execute("INSERT INTO workflows VALUES ('w', 'Old', '{}', 'then', 'then')")
        conn.execute("INSERT INTO runs (run_id, workflow_id, workflow_name, workflow_snapshot, status, created_at) "
                     "VALUES ('r1', 'w', 'Old', '{}', 'completed', 'then')")
    old.close()
    storage = Storage.open(path)
    assert storage.schema_version == MIGRATIONS[-1].version >= 2
    assert storage.get_run("r1")["status"] == "completed" and storage.list_workflows()[0]["name"] == "Old"
    assert storage.save_rulebook(book()) == (1, True)
    assert storage.db.query_one("SELECT rulebook_bot_id FROM runs WHERE run_id='r1'")["rulebook_bot_id"] is None
    storage.close()
