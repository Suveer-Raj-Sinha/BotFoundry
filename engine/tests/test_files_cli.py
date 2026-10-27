import json
import os
import subprocess
import sys

import pytest

from botfoundry import cli
from botfoundry.rules.constraints import SystemConstraints
from botfoundry.rules.paths import native_style

posix_only = pytest.mark.skipif(os.name == "nt", reason="uses POSIX symlinks")


@pytest.fixture()
def open_rules(monkeypatch):
    """Temporary folders sit under protected AppData on Windows, so these tests use their own system rules."""
    monkeypatch.setattr("botfoundry.rules.constraints.default_constraints",
                        lambda data_dir=None: SystemConstraints(protected_paths=(), too_broad_paths=(), style=native_style()))


@pytest.fixture()
def folder(tmp_path):
    root = tmp_path.resolve() / "stuff"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("same content here")
    (root / "sub" / "copy.txt").write_text("same content here")
    (root / "big1.bin").write_bytes(b"\x01" * 3000)
    (root / "sub" / "big1-backup.bin").write_bytes(b"\x01" * 3000)
    (root / "unique.txt").write_text("only one of me!")
    (root / "empty1.txt").write_text("")
    (root / "empty2.txt").write_text("")
    (root / ".hidden.txt").write_text("same content here")
    return root


def snapshot(root):
    return sorted((p.relative_to(root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file())


def run_cli(*args):
    return cli.main(["duplicates", *map(str, args)])


def test_reports_duplicate_groups_and_changes_nothing(folder, open_rules, capsys):
    before = snapshot(folder)
    assert run_cli(folder) == 0
    out = capsys.readouterr().out
    assert "Scanned 7 files" in out and "Found 2 groups of duplicates: 2 redundant files wasting" in out
    assert "2.9 KB x 2" in out and "17 B x 2" in out
    assert str(folder / "a.txt") in out and str(folder / "sub" / "copy.txt") in out
    assert ".hidden.txt" not in out and "empty1.txt" not in out                      # hidden and empty files are left out
    assert "Read-only: nothing was moved, changed or deleted." in out
    assert snapshot(folder) == before


def test_options(folder, open_rules, capsys):
    run_cli(folder, "--no-recursive")
    out = capsys.readouterr().out
    assert "No duplicates found." in out and "Scanned 5 files" in out               # a, big1, unique and the two empty files (hidden one skipped)
    run_cli(folder, "--include-hidden")
    assert "17 B x 3" in capsys.readouterr().out                                    # the hidden copy now counts
    run_cli(folder, "--min-size", "100")
    out = capsys.readouterr().out
    assert "2.9 KB x 2" in out and "17 B" not in out
    run_cli(folder, "--top", "1")
    out = capsys.readouterr().out
    assert "1. 2.9 KB x 2" in out and "2. " not in out and "and 1 more groups" in out


def test_keep_strategy_changes_the_suggestion(folder, open_rules, capsys):
    os.utime(folder / "sub" / "copy.txt", (1_000_000_000, 1_000_000_000))            # make this copy the oldest
    run_cli(folder, "--keep", "oldest")
    assert f"keep: {folder / 'sub' / 'copy.txt'}" in capsys.readouterr().out
    run_cli(folder, "--keep", "shortest_path")
    assert f"keep: {folder / 'a.txt'}" in capsys.readouterr().out


def test_json_output_is_complete_and_machine_readable(folder, open_rules, capsys):
    assert run_cli(folder, "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["folder"] == str(folder) and data["scan"]["files"] == 7 and data["skipped"] == [] and data["failed"] == []
    assert data["duplicates"]["stats"]["groups"] == 2
    assert {g["count"] for g in data["duplicates"]["groups"]} == {2}
    assert all(len(g["hash"]) == 64 for g in data["duplicates"]["groups"])


def test_a_folder_with_no_duplicates(folder, open_rules, capsys):
    (folder / "sub" / "copy.txt").write_text("now different")
    (folder / "sub" / "big1-backup.bin").write_bytes(b"\x02" * 3000)
    assert run_cli(folder) == 0
    assert "No duplicates found." in capsys.readouterr().out


@posix_only
def test_things_that_were_not_scanned_are_shown_not_hidden(folder, open_rules, capsys, tmp_path):
    (folder / "shortcut").symlink_to(tmp_path)
    run_cli(folder)
    out = capsys.readouterr().out
    assert "Not scanned: 1" in out and "shortcut" in out and "link or reparse point (not followed)" in out


def test_bad_arguments_are_refused_cleanly(tmp_path, folder):
    for target in (tmp_path / "missing", folder / "a.txt"):
        out = subprocess.run([sys.executable, "-m", "botfoundry", "duplicates", str(target)], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL)
        assert out.returncode == 2 and "Not a folder" in out.stderr and "Traceback" not in out.stderr


def test_dangerous_folders_are_refused_by_the_real_system_rules():
    root = os.path.abspath(os.sep)                                                   # C:\ or /
    out = subprocess.run([sys.executable, "-m", "botfoundry", "duplicates", root], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL)
    assert out.returncode == 1 and "rulebook.folder_too_broad" in out.stderr and "Traceback" not in out.stderr
    system = os.environ.get("SystemRoot") or "/etc"
    out = subprocess.run([sys.executable, "-m", "botfoundry", "duplicates", system], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL)
    assert out.returncode == 1 and "rulebook.folder_protected" in out.stderr
