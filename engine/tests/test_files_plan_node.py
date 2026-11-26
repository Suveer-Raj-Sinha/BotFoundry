"""files.plan_moves inside the real runner, on real temporary folders: the Rulebook is consulted, the disk is
only looked at (never changed), and a plan never contains a move the Rulebook would refuse."""
import asyncio
import os

import pytest
from pydantic import BaseModel

from botfoundry.rules.constraints import SystemConstraints
from botfoundry.rules.models import Rulebook
from botfoundry.rules.paths import native_style
from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.definition import WorkflowDefinition
from botfoundry.workflow.events import EventType
from botfoundry.workflow.node import Category, NodeResult, NodeSpec
from botfoundry.workflow.runner import Run, RunStatus
from botfoundry.workflow.types import DataType, PortSpec

posix_only = pytest.mark.skipif(os.name == "nt", reason="uses POSIX symlinks")
# Temporary folders live under protected system locations on Windows (AppData), so the tests use their own rules.
CONSTRAINTS = SystemConstraints(protected_paths=(), too_broad_paths=(), style=native_style())


class Forge(BaseModel):
    paths: list[str] = []
    garbage: bool = False


async def h_forge(ctx, cfg, inputs):
    if cfg.garbage:
        return NodeResult(outputs={"items": [1, 2, 3]})
    items = [{"path": p, "name": os.path.basename(p), "size": 1, "modified": "2026-03-04T05:06:07Z"} for p in cfg.paths]
    return NodeResult(outputs={"items": items})


def spec(node_id, handler, config, inputs, outputs, permissions=frozenset()):
    return NodeSpec(id=node_id, version=1, title=node_id, description="", category=Category.DATA, config_model=config,
                    inputs=inputs, outputs=outputs, handler=handler, permissions=permissions, timeout_seconds=30)


@pytest.fixture()
def registry():
    r = build_default_registry()
    r.register(spec("test.forge", h_forge, Forge, (PortSpec(name="start", required=False),),
                    (PortSpec(name="items", type=DataType.LIST),)))
    return r


@pytest.fixture()
def root(tmp_path):
    return tmp_path.resolve() / "data"


LAYOUT = {"a.jpg": "1", "b.PNG": "22", "report.pdf": "333", "notes.txt": "4444", "sub/c.jpg": "55555", "sub/data.csv": "6",
          "song.mp3": "7", "weird.xyz": "8", "sub/deeper/d.pdf": "9"}


def make_files(root, layout=LAYOUT):
    for rel, content in layout.items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())


def snapshot(root):
    return sorted((p.relative_to(root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file())


def tree(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def book(root, permissions=None, **changes):
    data = {"bot_id": "fixer", "name": "Fixer", "allowed_folders": [str(root)],
            "permissions": {"fs.read": "allow", "fs.move": "allow"} if permissions is None else permissions}
    data.update(changes)
    return Rulebook.model_validate(data)


def node(node_id, node_type, **config):
    return {"id": node_id, "type": node_type, "config": config}


def edge(source, source_port, target, target_port):
    return {"source": source, "source_port": source_port, "target": target, "target_port": target_port}


def definition(nodes, edges):
    return WorkflowDefinition.model_validate({"id": "w", "name": "W", "bot_id": "fixer",
                                              "nodes": [node("start", "core.manual_trigger"), *nodes], "edges": edges})


def plan_workflow(root, **plan_config):
    plan_config.setdefault("destination_root", str(root / "Organized"))
    return definition([node("scan", "files.scan_folder", folder=str(root)), node("plan", "files.plan_moves", **plan_config)],
                      [edge("start", "payload", "scan", "start"), edge("scan", "items", "plan", "items")])


def forged_workflow(paths, **plan_config):
    return definition([node("forge", "test.forge", paths=paths), node("plan", "files.plan_moves", **plan_config)],
                      [edge("start", "payload", "forge", "start"), edge("forge", "items", "plan", "items")])


def execute(defn, registry, rulebook, **kwargs):
    run = Run(defn, registry, rulebook=rulebook, rulebook_version=1 if rulebook else None, constraints=CONSTRAINTS, **kwargs)
    asyncio.run(run.execute())
    return run


def planned(run):
    return {os.path.basename(m["source"]): os.path.relpath(m["destination"], os.path.dirname(os.path.dirname(m["destination"])))
            for m in run.nodes["plan"].outputs["plan"]["moves"]}


def skipped(run):
    return {os.path.basename(s["path"]): s["code"] for s in run.nodes["plan"].outputs["skipped"]}


# ---- a plan from a real scan -------------------------------------------------------------------
def test_a_real_folder_is_planned_and_not_a_single_thing_changes(root, registry):
    make_files(root)
    before_files, before_tree = snapshot(root), tree(root)
    run = execute(plan_workflow(root), registry, book(root))
    assert run.status == RunStatus.COMPLETED, run.error
    out = run.nodes["plan"].outputs
    plan = out["plan"]
    assert plan["destination_root"] == str(root / "Organized") and plan["version"] == 1
    assert {os.path.basename(m["source"]): m["destination"] for m in plan["moves"]} == {
        "a.jpg": str(root / "Organized" / "Images" / "a.jpg"), "b.PNG": str(root / "Organized" / "Images" / "b.PNG"),
        "c.jpg": str(root / "Organized" / "Images" / "c.jpg"), "report.pdf": str(root / "Organized" / "Documents" / "report.pdf"),
        "notes.txt": str(root / "Organized" / "Documents" / "notes.txt"), "d.pdf": str(root / "Organized" / "Documents" / "d.pdf"),
        "data.csv": str(root / "Organized" / "Spreadsheets" / "data.csv"), "song.mp3": str(root / "Organized" / "Audio" / "song.mp3")}
    assert skipped(run) == {"weird.xyz": "rule.no_match"}
    assert out["stats"]["moves"] == 8 and out["stats"]["folders_to_create"] == 4
    assert run.nodes["plan"].summary == "Plan: 8 files to move (18 B); 1 left alone"
    assert snapshot(root) == before_files and tree(root) == before_tree             # nothing created, moved or changed
    assert not (root / "Organized").exists()


def test_planning_twice_gives_the_same_plan_and_a_changed_file_gives_a_different_one(root, registry):
    make_files(root)
    first = execute(plan_workflow(root), registry, book(root)).nodes["plan"].outputs["plan"]
    second = execute(plan_workflow(root), registry, book(root)).nodes["plan"].outputs["plan"]
    assert first == second
    (root / "a.jpg").write_bytes(b"now longer")
    third = execute(plan_workflow(root), registry, book(root)).nodes["plan"].outputs["plan"]
    assert third["plan_id"] != first["plan_id"]


def test_hashes_from_the_hash_node_travel_into_the_plan(root, registry):
    make_files(root, {"a.jpg": "same", "b.jpg": "same", "c.jpg": "different"})
    defn = definition([node("scan", "files.scan_folder", folder=str(root)), node("hash", "files.hash"),
                       node("plan", "files.plan_moves", destination_root=str(root / "Organized"))],
                      [edge("start", "payload", "scan", "start"), edge("scan", "items", "hash", "items"),
                       edge("hash", "items", "plan", "items")])
    run = execute(defn, registry, book(root))
    hashes = {os.path.basename(m["source"]): m["hash"] for m in run.nodes["plan"].outputs["plan"]["moves"]}
    assert hashes["a.jpg"] == hashes["b.jpg"] != hashes["c.jpg"] and len(hashes["a.jpg"]) == 64


def test_custom_rules_dates_and_an_unmatched_folder_through_the_node(root, registry):
    make_files(root)
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (1_750_000_000, 1_750_000_000))                                # 2025-06-15 in UTC
    run = execute(plan_workflow(
        root, rules=[{"folder": "Work/Invoices", "name_contains": ["report"], "label": "reports"}],
        unmatched_folder="Misc", date_folders="year_month"), registry, book(root))
    assert run.status == RunStatus.COMPLETED, run.error
    organized = root / "Organized"
    by_name = {os.path.basename(m["source"]): m for m in run.nodes["plan"].outputs["plan"]["moves"]}
    assert by_name["report.pdf"]["destination"] == str(organized / "Work" / "Invoices" / "2025-06" / "report.pdf")
    assert by_name["report.pdf"]["rule"] == "reports" and by_name["report.pdf"]["category"] == "Work/Invoices"
    assert by_name["a.jpg"]["destination"] == str(organized / "Images" / "2025-06" / "a.jpg")
    assert by_name["weird.xyz"]["destination"] == str(organized / "Misc" / "2025-06" / "weird.xyz")
    assert run.nodes["plan"].outputs["skipped"] == [] and len(by_name) == 9


def test_a_plan_over_more_files_than_one_rulebook_question_can_hold(root, registry):
    make_files(root, {f"f{i:04d}.jpg": str(i) for i in range(1100)})                # asked in chunks of 500 paths
    run = execute(plan_workflow(root), registry, book(root))
    assert run.status == RunStatus.COMPLETED, run.error
    assert run.nodes["plan"].outputs["stats"]["moves"] == 1100 and run.nodes["plan"].outputs["skipped"] == []


# ---- the Rulebook decides -----------------------------------------------------------------------
def test_without_permission_to_move_there_is_nothing_to_plan(root, registry):
    make_files(root)
    for permissions, code in (({"fs.read": "allow"}, "rulebook.not_granted"), ({"fs.read": "allow", "fs.move": "deny"}, "rulebook.denied")):
        run = execute(plan_workflow(root), registry, book(root, permissions))
        error = run.nodes["plan"].error
        assert run.status == RunStatus.FAILED and error["code"] == code, code
        assert error["message"].startswith("There is nothing to plan:") and error["details"] == {"permission": "fs.move"}


def test_planning_never_pauses_for_approval_even_when_moving_will_need_it(root, registry):
    make_files(root)
    run = execute(plan_workflow(root), registry, book(root, {"fs.read": "allow", "fs.move": "ask"}))
    assert run.status == RunStatus.COMPLETED, run.error
    assert run.nodes["plan"].outputs["plan"]["approval_required"] is True
    assert not [e for e in run.events.all() if e.type == EventType.APPROVAL_REQUESTED]          # a plan is only a proposal
    allowed = execute(plan_workflow(root), registry, book(root))
    assert allowed.nodes["plan"].outputs["plan"]["approval_required"] is False


def test_a_destination_outside_the_approved_folders_is_refused(root, registry, tmp_path):
    make_files(root)
    run = execute(plan_workflow(root, destination_root=str(tmp_path.resolve() / "elsewhere")), registry, book(root))
    error = run.nodes["plan"].error
    assert run.status == RunStatus.FAILED and error["code"] == "path.outside_allowed"
    assert error["message"].startswith("The destination folder is not allowed")


def test_a_destination_inside_a_forbidden_folder_is_refused(root, registry):
    make_files(root)
    run = execute(plan_workflow(root), registry, book(root, forbidden_folders=[str(root / "Organized")]))
    assert run.status == RunStatus.FAILED and run.nodes["plan"].error["code"] == "path.forbidden"


def test_only_the_forbidden_category_folder_is_skipped(root, registry):
    make_files(root)
    run = execute(plan_workflow(root), registry, book(root, forbidden_folders=[str(root / "Organized" / "Images")]))
    assert run.status == RunStatus.COMPLETED, run.error
    assert skipped(run) == {"a.jpg": "path.forbidden", "b.PNG": "path.forbidden", "c.jpg": "path.forbidden", "weird.xyz": "rule.no_match"}
    assert len(run.nodes["plan"].outputs["plan"]["moves"]) == 5


def test_a_source_the_rulebook_forbids_is_never_planned(root, registry):
    make_files(root)
    run = execute(plan_workflow(root), registry, book(root, forbidden_folders=[str(root / "sub")]))
    names = {os.path.basename(m["source"]) for m in run.nodes["plan"].outputs["plan"]["moves"]}
    assert "c.jpg" not in names and "data.csv" not in names and "a.jpg" in names           # 'sub' was never even scanned


def test_a_forged_record_outside_the_approved_folders_is_skipped_with_the_reason(root, registry, tmp_path):
    make_files(root)
    outside = tmp_path.resolve() / "elsewhere" / "stolen.jpg"
    inside = root / "a.jpg"
    run = execute(forged_workflow([str(outside), str(inside)], destination_root=str(root / "Organized")), registry, book(root))
    assert run.status == RunStatus.COMPLETED, run.error
    assert skipped(run) == {"stolen.jpg": "path.outside_allowed"}
    reason = run.nodes["plan"].outputs["skipped"][0]["reason"]
    assert reason.startswith("Not allowed to move this file") and [os.path.basename(m["source"]) for m in run.nodes["plan"].outputs["plan"]["moves"]] == ["a.jpg"]


@posix_only
def test_a_destination_folder_that_is_a_link_leading_outside_is_refused(root, registry, tmp_path):
    make_files(root)
    elsewhere = tmp_path.resolve() / "elsewhere"
    elsewhere.mkdir()
    (root / "Organized").mkdir()
    (root / "Organized" / "Images").symlink_to(elsewhere)
    run = execute(plan_workflow(root), registry, book(root))
    assert run.status == RunStatus.COMPLETED, run.error
    assert skipped(run)["a.jpg"] == "path.escapes_via_link" and skipped(run)["c.jpg"] == "path.escapes_via_link"
    assert "Documents" in {m["category"] for m in run.nodes["plan"].outputs["plan"]["moves"]}      # other folders are fine
    assert list(elsewhere.iterdir()) == []


def test_the_run_limit_is_a_warning_on_the_plan_not_a_silent_cut(root, registry):
    make_files(root)
    run = execute(plan_workflow(root), registry, book(root, limits={"max_files_per_run": 3}))
    plan = run.nodes["plan"].outputs["plan"]
    assert run.status == RunStatus.COMPLETED and len(plan["moves"]) == 8                     # nothing was trimmed
    assert [w["code"] for w in plan["warnings"]] == ["limit.max_files_per_run"] and "allows 3" in plan["warnings"][0]["message"]
    assert execute(plan_workflow(root), registry, book(root)).nodes["plan"].outputs["plan"]["warnings"] == []


# ---- never overwrite ------------------------------------------------------------------------------
def test_an_existing_file_at_the_destination_is_reported_and_left_untouched(root, registry):
    make_files(root)
    make_files(root, {"Organized/Images/a.jpg": "precious original"})
    run = execute(plan_workflow(root), registry, book(root))
    by_path = {s["path"]: s["code"] for s in run.nodes["plan"].outputs["skipped"]}
    assert by_path[str(root / "a.jpg")] == "destination.exists"                              # would have overwritten it
    assert by_path[str(root / "Organized" / "Images" / "a.jpg")] == "already_in_place"      # the existing file itself
    assert (root / "Organized" / "Images" / "a.jpg").read_text() == "precious original"
    assert str(root / "a.jpg") not in {m["source"] for m in run.nodes["plan"].outputs["plan"]["moves"]}
    assert "b.PNG" in {os.path.basename(m["source"]) for m in run.nodes["plan"].outputs["plan"]["moves"]}


def test_a_file_that_is_in_the_way_of_a_category_folder(root, registry):
    make_files(root)
    make_files(root, {"Organized/Images": "I am a file, not a folder"})
    run = execute(plan_workflow(root), registry, book(root))
    assert run.status == RunStatus.COMPLETED, run.error
    assert skipped(run)["a.jpg"] == "destination.folder_blocked" and skipped(run)["c.jpg"] == "destination.folder_blocked"
    assert (root / "Organized" / "Images").read_text() == "I am a file, not a folder"
    assert "Documents" in {m["category"] for m in run.nodes["plan"].outputs["plan"]["moves"]}


def test_same_name_in_two_folders_moves_neither(root, registry):
    make_files(root, {"x/report.pdf": "one", "y/report.pdf": "two", "z/other.pdf": "three"})
    run = execute(plan_workflow(root), registry, book(root))
    by_path = {s["path"]: s["code"] for s in run.nodes["plan"].outputs["skipped"]}
    assert by_path == {str(root / "x" / "report.pdf"): "destination.collision", str(root / "y" / "report.pdf"): "destination.collision"}
    assert [os.path.basename(m["source"]) for m in run.nodes["plan"].outputs["plan"]["moves"]] == ["other.pdf"]


def test_files_already_sorted_are_recognised_on_a_second_look(root, registry):
    make_files(root, {"Organized/Images/a.jpg": "1", "Organized/Documents/r.pdf": "2", "new.jpg": "3"})
    run = execute(plan_workflow(root), registry, book(root))
    assert skipped(run) == {"a.jpg": "already_in_place", "r.pdf": "already_in_place"}
    assert [os.path.basename(m["source"]) for m in run.nodes["plan"].outputs["plan"]["moves"]] == ["new.jpg"]


# ---- settings -------------------------------------------------------------------------------------
@pytest.mark.parametrize("config", [
    {"destination_root": "/x", "rules": [{"folder": "..", "extensions": [".a"]}]},
    {"destination_root": "/x", "rules": [{"folder": "ok"}]},                                   # a rule with no criterion
    {"destination_root": "/x", "rules": [{"folder": "NUL", "extensions": [".a"]}]},
    {"destination_root": "/x", "unmatched_folder": "bad<name"},
    {"destination_root": "/x", "date_folders": "weekly"},
    {"destination_root": "/x", "use_default_rules": False},                                    # nothing to sort by
    {"destination_root": ""},
    {"destination_root": "/x", "surprise": 1}])
def test_settings_that_make_no_sense_are_rejected_before_running(root, registry, config):
    run = execute(definition([node("plan", "files.plan_moves", **config)], []), registry, book(root))
    assert run.error["code"] == "validation_failed"
    assert "node.invalid_config" in {i["code"] for i in run.events.all()[2].payload["issues"]}


def test_a_misspelled_setting_is_an_error_not_silently_ignored(root, registry):
    run = execute(definition([node("plan", "files.plan_moves", destination_root="/x", unmatched_folders="Misc")], []),
                  registry, book(root))
    issue = next(i for i in run.events.all()[2].payload["issues"] if i["code"] == "node.invalid_config")
    assert "unmatched_folders" in str(issue)


def test_a_blank_unmatched_folder_means_leave_them(root, registry):
    make_files(root)
    run = execute(plan_workflow(root, unmatched_folder="   "), registry, book(root))
    assert skipped(run) == {"weird.xyz": "rule.no_match"}


def test_no_default_rules_and_no_custom_rules_but_an_unmatched_folder_moves_everything_there(root, registry):
    make_files(root, {"a.jpg": "1", "b.txt": "2"})
    run = execute(plan_workflow(root, use_default_rules=False, unmatched_folder="Everything"), registry, book(root))
    assert {m["category"] for m in run.nodes["plan"].outputs["plan"]["moves"]} == {"Everything"}


def test_a_relative_destination_fails_with_a_plain_message(root, registry):
    make_files(root)
    run = execute(plan_workflow(root, destination_root="Organized"), registry, book(root))
    error = run.nodes["plan"].error
    assert run.status == RunStatus.FAILED and error["code"] == "plan.bad_destination" and "absolute" in error["message"]


def test_the_plan_node_rejects_input_that_is_not_file_records(root, registry):
    run = execute(definition([node("forge", "test.forge", garbage=True), node("plan", "files.plan_moves", destination_root=str(root / "O"))],
                             [edge("start", "payload", "forge", "start"), edge("forge", "items", "plan", "items")]),
                  registry, book(root))
    assert run.status == RunStatus.FAILED and run.nodes["plan"].error["code"] == "files.bad_input"


def test_an_empty_input_is_a_valid_empty_plan(root, registry):
    run = execute(forged_workflow([], destination_root=str(root / "O")), registry, book(root))
    assert run.status == RunStatus.COMPLETED and run.nodes["plan"].outputs["plan"]["moves"] == []
    assert run.nodes["plan"].summary == "Plan: 0 files to move (0 B)"


def test_a_rulebook_check_that_misanswers_stops_the_plan_instead_of_guessing(root, registry, monkeypatch):
    make_files(root)
    real = Run._preflighter

    def faulty(self):
        honest = real(self)

        def preflight(permission, paths=()):
            answer = honest(permission, paths)
            return answer if len(paths) <= 1 else type(answer)(answer.outcome, answer.code, answer.message, answer.verdicts[:-1])
        return preflight

    monkeypatch.setattr(Run, "_preflighter", faulty)
    run = execute(plan_workflow(root), registry, book(root))
    assert run.status == RunStatus.FAILED and run.nodes["plan"].error["code"] == "plan.preflight_failed"


# ---- the shipped example --------------------------------------------------------------------------------
def test_the_example_workflow_plans_a_real_folder_with_the_example_rulebook(root, registry):
    import json
    from pathlib import Path
    examples = Path(__file__).resolve().parents[2] / "examples"
    make_files(root, {"Invoice-7.pdf": "pay me", "photo.jpg": "pixels", "notes.txt": "hi", "odd.xyz": "?"})
    text = (examples / "organize-downloads.workflow.json").read_text(encoding="utf-8")
    text = text.replace(json.dumps("C:\\Users\\YourName\\Downloads\\Organized")[1:-1], json.dumps(str(root / "Organized"))[1:-1])
    text = text.replace(json.dumps("C:\\Users\\YourName\\Downloads")[1:-1], json.dumps(str(root))[1:-1])
    assert "YourName" not in json.dumps(json.loads(text)["nodes"])                       # every folder was replaced
    rulebook = json.loads((examples / "downloads-cleaner.rulebook.json").read_text(encoding="utf-8"))
    rulebook.update(allowed_folders=[str(root)], forbidden_folders=[str(root / "Keep")])
    run = execute(WorkflowDefinition.model_validate(json.loads(text)), registry, Rulebook.model_validate(rulebook))
    assert run.status == RunStatus.COMPLETED, run.error
    plan = run.nodes["plan"].outputs["plan"]
    assert {m["name"]: m["category"] for m in plan["moves"]} == {"Invoice-7.pdf": "Invoices", "photo.jpg": "Images", "notes.txt": "Documents"}
    assert all(len(m["hash"]) == 64 for m in plan["moves"]) and plan["approval_required"] is True
    assert skipped(run) == {"odd.xyz": "rule.no_match"}
