import json
import os
import subprocess
import sys

import pytest

from botfoundry import cli
from botfoundry.rules.constraints import SystemConstraints
from botfoundry.rules.paths import native_style


@pytest.fixture()
def open_rules(monkeypatch):
    """Temporary folders sit under protected AppData on Windows, so these tests use their own system rules."""
    monkeypatch.setattr("botfoundry.rules.constraints.default_constraints",
                        lambda data_dir=None: SystemConstraints(protected_paths=(), too_broad_paths=(), style=native_style()))


@pytest.fixture()
def folder(tmp_path):
    root = tmp_path.resolve() / "downloads"
    for rel, text in {"a.jpg": "1", "b.PNG": "22", "report.pdf": "333", "sub/c.jpg": "4444", "sub/invoice-3.pdf": "55555",
                      "data.csv": "6", "weird.xyz": "7", ".hidden.jpg": "8"}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def snapshot(root):
    return sorted((p.relative_to(root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*"))


def run_cli(*args):
    return cli.main(["organize", *map(str, args)])


def test_preview_shows_the_plan_and_changes_nothing(folder, open_rules, capsys):
    before = snapshot(folder)
    assert run_cli(folder) == 0
    out = capsys.readouterr().out
    assert "Scanned 7 files" in out and f"Sorting into: {folder / 'Organized'}" in out
    assert "Would move 6 files" in out and "1 would stay where they are" in out
    for line in ("Images", "Documents", "Spreadsheets"):
        assert line in out
    assert f"{folder / 'a.jpg'}" in out and os.path.join("Images", "a.jpg") in out
    assert "no rule matches them  [rule.no_match]" in out
    assert "Folders that would be created: 3" in out
    assert "Moving these files would need your approval." in out
    assert "Plan id: " in out and "Preview only: nothing was moved, changed or deleted." in out
    assert snapshot(folder) == before and not (folder / "Organized").exists()
    assert ".hidden.jpg" not in out                                                          # hidden files are not scanned


def test_destination_can_be_somewhere_else(folder, open_rules, capsys, tmp_path):
    target = tmp_path.resolve() / "sorted"
    assert run_cli(folder, "--to", target) == 0
    assert f"Sorting into: {target}" in capsys.readouterr().out
    assert not target.exists()


def test_json_output_has_the_whole_plan(folder, open_rules, capsys):
    assert run_cli(folder, "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["folder"] == str(folder) and data["scan"]["files"] == 7 and data["not_scanned"] == []
    plan = data["plan"]
    assert len(plan["plan_id"]) == 64 and plan["approval_required"] is True and plan["warnings"] == []
    assert {os.path.basename(m["source"]): m["category"] for m in plan["moves"]} == {
        "a.jpg": "Images", "b.PNG": "Images", "c.jpg": "Images", "report.pdf": "Documents", "invoice-3.pdf": "Documents",
        "data.csv": "Spreadsheets"}
    assert [s["code"] for s in data["skipped"]] == ["rule.no_match"] and data["stats"]["moves"] == 6


def test_the_same_folder_gives_the_same_plan_id(folder, open_rules, capsys):
    run_cli(folder, "--json")
    first = json.loads(capsys.readouterr().out)["plan"]["plan_id"]
    run_cli(folder, "--json")
    assert json.loads(capsys.readouterr().out)["plan"]["plan_id"] == first


def test_your_own_rules_file(folder, open_rules, capsys, tmp_path):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": [{"folder": "Work/Invoices", "name_contains": ["invoice"]}], "unmatched_folder": "Misc"}))
    assert run_cli(folder, "--rules", rules, "--json") == 0
    plan = json.loads(capsys.readouterr().out)["plan"]
    by_name = {os.path.basename(m["source"]): m["category"] for m in plan["moves"]}
    assert by_name["invoice-3.pdf"] == "Work/Invoices" and by_name["weird.xyz"] == "Misc" and by_name["report.pdf"] == "Documents"


def test_flags_override_and_combine(folder, open_rules, capsys):
    assert run_cli(folder, "--no-default-rules", "--unmatched-folder", "Everything", "--date-folders", "year", "--json") == 0
    plan = json.loads(capsys.readouterr().out)["plan"]
    assert {m["category"] for m in plan["moves"]} == {"Everything"} and len(plan["moves"]) == 7
    assert all(os.path.basename(os.path.dirname(m["destination"])).isdigit() for m in plan["moves"])      # dated subfolders


def test_options_for_what_is_scanned(folder, open_rules, capsys):
    run_cli(folder, "--no-recursive", "--include-hidden", "--json")
    data = json.loads(capsys.readouterr().out)
    assert data["scan"]["files"] == 6 and ".hidden.jpg" in {os.path.basename(m["source"]) for m in data["plan"]["moves"]}


def test_top_limits_the_examples(folder, open_rules, capsys):
    run_cli(folder, "--top", "2")
    out = capsys.readouterr().out
    assert "First 2 moves:" in out and "and 4 more" in out


def test_a_second_look_after_a_manual_sort_recognises_what_is_in_place(folder, open_rules, capsys):
    (folder / "Organized" / "Images").mkdir(parents=True)
    (folder / "a.jpg").rename(folder / "Organized" / "Images" / "a.jpg")
    run_cli(folder)
    out = capsys.readouterr().out
    assert "already in place  [already_in_place]" in out and "Would move 5 files" in out


def test_collisions_are_explained(folder, open_rules, capsys):
    (folder / "other").mkdir()
    (folder / "other" / "a.jpg").write_text("a different a")
    run_cli(folder)
    out = capsys.readouterr().out
    assert "two files would share a destination  [destination.collision]" in out and "none of them is moved" in out


def test_nothing_to_do_is_said_plainly(tmp_path, open_rules, capsys):
    empty = tmp_path.resolve() / "empty"
    empty.mkdir()
    (empty / "x.xyz").write_text("?")
    assert run_cli(empty) == 0
    out = capsys.readouterr().out
    assert "Nothing would be moved." in out and "Moving these files would need your approval" not in out


def test_the_run_limit_warning_is_shown(folder, open_rules, capsys, monkeypatch):
    import botfoundry.rules.models as models

    class Limited(models.Rulebook):                                  # the preview's temporary Rulebook, with a small limit
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, "limits": {"max_files_per_run": 2}})

    monkeypatch.setattr(models, "Rulebook", Limited)
    assert run_cli(folder) == 0
    assert "Warning: The plan moves 6 files but this bot's Rulebook allows 2 per run" in capsys.readouterr().out


# ---- refusing things cleanly ------------------------------------------------------------------------
def test_bad_arguments_are_refused_cleanly(tmp_path, folder):
    for args in ([str(tmp_path / "missing")], [str(folder / "a.jpg")]):
        out = subprocess.run([sys.executable, "-m", "botfoundry", "organize", *args], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL)
        assert out.returncode == 2 and "Not a folder" in out.stderr and "Traceback" not in out.stderr


def test_a_bad_rules_file_is_refused_cleanly(folder, open_rules, tmp_path, capsys):
    for text, message in (("not json", "Cannot read the rules file"), ("[1, 2]", "must contain a JSON object"),
                          ('{"rulez": []}', "Unknown setting(s) in the rules file: rulez")):
        rules = tmp_path / "bad.json"
        rules.write_text(text)
        assert cli.main(["organize", str(folder), "--rules", str(rules)]) == 2
        assert message in capsys.readouterr().err
    assert cli.main(["organize", str(folder), "--rules", str(tmp_path / "missing.json")]) == 2


def test_settings_that_cannot_work_are_explained(folder, open_rules, tmp_path, capsys):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": [{"folder": "..", "extensions": [".jpg"]}]}))
    assert cli.main(["organize", str(folder), "--rules", str(rules)]) == 1
    err = capsys.readouterr().err
    assert "The settings cannot be used" in err and "node.invalid_config" in err and "Traceback" not in err


def test_dangerous_folders_are_refused_by_the_real_system_rules(folder):
    root = os.path.abspath(os.sep)                                                              # C:\ or /
    out = subprocess.run([sys.executable, "-m", "botfoundry", "organize", root], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL)
    assert out.returncode == 1 and "rulebook.folder_too_broad" in out.stderr and "Traceback" not in out.stderr
    system = os.environ.get("SystemRoot") or "/etc"                                             # a protected system folder as destination
    out = subprocess.run([sys.executable, "-m", "botfoundry", "organize", str(folder), "--to", os.path.join(system, "sorted")],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert out.returncode == 1 and "rulebook.folder_protected" in out.stderr and "Traceback" not in out.stderr
