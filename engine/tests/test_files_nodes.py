import asyncio
import json
import time

import pytest
from pydantic import BaseModel

from botfoundry.files import nodes as file_nodes
from botfoundry.files.blocking import run_blocking
from botfoundry.files.fsutil import StopFlag
from botfoundry.rules.constraints import SystemConstraints
from botfoundry.rules.models import Rulebook
from botfoundry.rules.paths import native_style
from botfoundry.storage import Storage
from botfoundry.storage.repository import MAX_STORED_OUTPUT_BYTES, stored_outputs
from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.definition import WorkflowDefinition
from botfoundry.workflow.node import Category, NodeResult, NodeSpec
from botfoundry.workflow.runner import NodeStatus, Run, RunStatus
from botfoundry.workflow.types import DataType, PortSpec

# Temporary folders live under protected system locations on Windows (AppData), so the tests use their own rules.
CONSTRAINTS = SystemConstraints(protected_paths=(), too_broad_paths=(), style=native_style())
OBSERVED: dict = {}


class Forge(BaseModel):
    path: str = ""
    size: int = 1
    garbage: bool = False


class Delete(BaseModel):
    path: str


async def h_forge(ctx, cfg, inputs):
    items = [1, 2, 3] if cfg.garbage else [{"path": cfg.path, "name": "forged.txt", "stem": "forged", "extension": ".txt",
                                           "size": cfg.size, "modified": None, "hash": None}]
    return NodeResult(outputs={"items": items})


async def h_delete(ctx, cfg, inputs):
    import os
    os.unlink(cfg.path)
    return NodeResult(outputs={"items": inputs["items"]})


async def h_blocking(ctx, cfg, inputs):
    def work(stop):
        OBSERVED["started"] = True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if stop():
                OBSERVED["stopped_at"] = time.monotonic()
                return None
            time.sleep(0.01)
    await run_blocking(ctx, work)
    return NodeResult(outputs={"result": 1})


def spec(node_id, handler, config, inputs, outputs, timeout=30):
    return NodeSpec(id=node_id, version=1, title=node_id, description="", category=Category.DATA, config_model=config,
                    inputs=inputs, outputs=outputs, handler=handler, timeout_seconds=timeout)


@pytest.fixture()
def registry():
    OBSERVED.clear()
    r = build_default_registry()
    lst = (PortSpec(name="items", type=DataType.LIST),)
    r.register(spec("test.forge", h_forge, Forge, (PortSpec(name="start", required=False),), lst))
    r.register(spec("test.delete", h_delete, Delete, lst, lst))
    r.register(spec("test.blocking", h_blocking, Forge, (PortSpec(name="start", required=False),),
                    (PortSpec(name="result"),), timeout=60))
    return r


@pytest.fixture()
def root(tmp_path):
    return tmp_path.resolve() / "data"


def make_files(root, layout):
    for rel, content in layout.items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())


def book(root, **changes):
    data = {"bot_id": "fixer", "name": "Fixer", "allowed_folders": [str(root)], "permissions": {"fs.read": "allow"}}
    data.update(changes)
    return Rulebook.model_validate(data)


def node(node_id, node_type, **config):
    return {"id": node_id, "type": node_type, "config": config}


def edge(source, source_port, target, target_port):
    return {"source": source, "source_port": source_port, "target": target, "target_port": target_port}


def definition(nodes, edges):
    return WorkflowDefinition.model_validate({
        "id": "w", "name": "W", "bot_id": "fixer",
        "nodes": [node("start", "core.manual_trigger"), *nodes], "edges": edges})


def pipeline(folder, *, hash_config=None, scan_config=None):
    return definition(
        [node("scan", "files.scan_folder", folder=str(folder), **(scan_config or {})),
         node("hash", "files.hash", skip_unique_sizes=True, **(hash_config or {})),
         node("dups", "files.find_duplicates")],
        [edge("start", "payload", "scan", "start"), edge("scan", "items", "hash", "items"),
         edge("hash", "items", "dups", "items")])


def execute(defn, registry, rulebook=None, **kwargs):
    run = Run(defn, registry, rulebook=rulebook, rulebook_version=1 if rulebook else None, constraints=CONSTRAINTS, **kwargs)
    asyncio.run(run.execute())
    return run


LAYOUT = {"a.txt": "hello", "b.txt": "hello", "c.txt": "world!!", "sub/d.txt": "hello", "sub/big.bin": b"z" * 5000,
          "sub/other.bin": b"y" * 5000}


# ---- the whole pipeline ---------------------------------------------------------------------
def test_the_pipeline_finds_duplicates_end_to_end(root, registry):
    make_files(root, LAYOUT)
    run = execute(pipeline(root), registry, book(root))
    assert run.status == RunStatus.COMPLETED, run.error
    scan = run.nodes["scan"].outputs
    assert scan["stats"]["files"] == 6 and scan["skipped"] == []
    hashed = run.nodes["hash"].outputs
    assert hashed["stats"]["hashed"] == 5 and hashed["stats"]["not_needed"] == 1       # "world!!" has a unique size
    assert run.nodes["hash"].summary.startswith("Hashed 5 files")
    dups = run.nodes["dups"].outputs
    assert dups["stats"] == {"groups": 1, "duplicate_files": 2, "wasted_bytes": 10, "files_examined": 6}
    group = dups["groups"][0]
    assert group["count"] == 3 and sorted(i["name"] for i in [group["keep"], *group["duplicates"]]) == ["a.txt", "b.txt", "d.txt"]
    assert "2 redundant files" in run.nodes["dups"].summary
    json.dumps({k: v.outputs for k, v in run.nodes.items()})                           # everything is JSON-friendly


def test_nothing_on_disk_is_changed_by_the_pipeline(root, registry):
    make_files(root, LAYOUT)
    before = sorted((p.relative_to(root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file())
    execute(pipeline(root), registry, book(root))
    after = sorted((p.relative_to(root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file())
    assert before == after and sorted(p.name for p in root.rglob("*") if p.is_dir()) == ["sub"]


# ---- the Rulebook decides what may be scanned or read ---------------------------------------
def test_a_folder_outside_the_approved_folders_is_refused(root, tmp_path, registry):
    make_files(root, LAYOUT)
    other = tmp_path.resolve() / "elsewhere"
    make_files(other, {"x.txt": "x"})
    run = execute(pipeline(other), registry, book(root))
    assert run.status == RunStatus.FAILED and run.nodes["scan"].error["code"] == "path.outside_allowed"
    assert run.nodes["hash"].status == NodeStatus.SKIPPED


def test_forbidden_subfolders_are_skipped_and_reported(root, registry):
    make_files(root, LAYOUT)
    run = execute(pipeline(root), registry, book(root, forbidden_folders=[str(root / "sub")]))
    assert run.status == RunStatus.COMPLETED
    out = run.nodes["scan"].outputs
    assert sorted(i["name"] for i in out["items"]) == ["a.txt", "b.txt", "c.txt"]
    assert [(s["path"], "forbidden" in s["reason"]) for s in out["skipped"]] == [(str(root / "sub"), True)]


def test_without_fs_read_nothing_is_scanned(root, registry):
    make_files(root, LAYOUT)
    for permissions in ({"fs.read": "deny"}, {}):
        run = execute(pipeline(root), registry, book(root, permissions=permissions))
        assert run.status == RunStatus.FAILED and run.nodes["scan"].error["code"] == "permission.denied"
        assert run.nodes["scan"].outputs == {}


def test_scanning_needs_a_rulebook(root, registry):
    make_files(root, LAYOUT)
    run = execute(pipeline(root), registry, None)
    assert run.status == RunStatus.FAILED and run.error["code"] == "validation_failed"
    assert run.nodes["scan"].status == NodeStatus.PENDING


def test_a_workflow_cannot_make_the_hash_node_read_files_it_was_never_allowed(root, tmp_path, registry, monkeypatch):
    """Records can be forged by a workflow; the hash node must re-check every path before it reads anything."""
    make_files(root, LAYOUT)
    secret = tmp_path.resolve() / "secrets" / "passwords.txt"
    make_files(secret.parent, {"passwords.txt": "hunter2"})
    read = []
    monkeypatch.setattr(file_nodes, "hash_items", lambda *a, **k: read.append(a) or {"items": [], "failed": [], "stats": {}})
    defn = definition([node("forge", "test.forge", path=str(secret), size=7), node("hash", "files.hash")],
                      [edge("start", "payload", "forge", "start"), edge("forge", "items", "hash", "items")])
    run = execute(defn, registry, book(root))
    assert run.status == RunStatus.FAILED and run.nodes["hash"].error["code"] == "path.outside_allowed"
    assert read == []                                                                   # nothing was even attempted
    assert run.nodes["hash"].error["details"]["refused_paths"][0]["path"] == str(secret)


def test_the_file_nodes_reject_input_that_is_not_file_records(root, registry):
    make_files(root, LAYOUT)
    for node_type, config in (("files.hash", {}), ("files.filter", {"extensions": [".txt"]}), ("files.find_duplicates", {})):
        defn = definition([node("forge", "test.forge", garbage=True), node("n", node_type, **config)],
                          [edge("start", "payload", "forge", "start"), edge("forge", "items", "n", "items")])
        run = execute(defn, registry, book(root))
        assert run.status == RunStatus.FAILED and run.nodes["n"].error["code"] == "files.bad_input", node_type


def test_duplicates_refuses_unhashed_records(root, registry):
    make_files(root, LAYOUT)
    defn = definition([node("scan", "files.scan_folder", folder=str(root)), node("dups", "files.find_duplicates")],
                      [edge("start", "payload", "scan", "start"), edge("scan", "items", "dups", "items")])
    run = execute(defn, registry, book(root))
    assert run.status == RunStatus.FAILED and run.nodes["dups"].error["code"] == "duplicates.not_hashed"


# ---- honest reporting of what went wrong --------------------------------------------------
def test_missing_and_non_folder_scan_targets_fail_clearly(root, registry):
    make_files(root, LAYOUT)
    for target in (root / "nope", root / "a.txt"):
        run = execute(pipeline(target), registry, book(root))
        assert run.status == RunStatus.FAILED and run.nodes["scan"].error["code"] == "scan.folder_not_found", target


def test_too_many_files_fails_instead_of_truncating(root, registry):
    make_files(root, LAYOUT)
    run = execute(pipeline(root, scan_config={"max_items": 3}), registry, book(root))
    error = run.nodes["scan"].error
    assert run.status == RunStatus.FAILED and error["code"] == "scan.too_many_files" and error["details"] == {"max_items": 3}


def test_a_file_deleted_between_scan_and_hash_is_reported(root, registry):
    make_files(root, LAYOUT)
    defn = definition(
        [node("scan", "files.scan_folder", folder=str(root)), node("del", "test.delete", path=str(root / "b.txt")),
         node("hash", "files.hash")],
        [edge("start", "payload", "scan", "start"), edge("scan", "items", "del", "items"), edge("del", "items", "hash", "items")])
    run = execute(defn, registry, book(root))
    assert run.status == RunStatus.COMPLETED
    out = run.nodes["hash"].outputs
    assert out["failed"] == [{"path": str(root / "b.txt"), "reason": "vanished"}]
    assert "b.txt" not in [i["name"] for i in out["items"]] and "1 could not be read" in run.nodes["hash"].summary


# ---- the filter node inside a workflow ------------------------------------------------------
def test_the_filter_node_splits_a_scan(root, registry):
    make_files(root, LAYOUT)
    defn = definition(
        [node("scan", "files.scan_folder", folder=str(root)), node("f", "files.filter", extensions=["txt"], min_size=5)],
        [edge("start", "payload", "scan", "start"), edge("scan", "items", "f", "items")])
    run = execute(defn, registry, book(root))
    out = run.nodes["f"].outputs
    assert sorted(i["name"] for i in out["matched"]) == ["a.txt", "b.txt", "c.txt", "d.txt"]
    assert out["stats"] == {"matched": 4, "rejected": 2}


@pytest.mark.parametrize("config", [{}, {"name_regex": "(unclosed"}, {"min_size": 10, "max_size": 5}, {"extensions": "txt"}])
def test_filter_configurations_that_make_no_sense_are_rejected_before_running(root, registry, config):
    defn = definition([node("f", "files.filter", **config)], [])
    run = execute(defn, registry, book(root))
    assert run.error["code"] == "validation_failed"
    assert "node.invalid_config" in {i["code"] for i in run.events.all()[2].payload["issues"]}


# ---- cancellation and threads ---------------------------------------------------------------
def test_blocking_work_stops_when_the_run_is_cancelled(registry, root, tmp_path):
    defn = definition([node("w", "test.blocking")], [edge("start", "payload", "w", "start")])
    run = Run(defn, registry, rulebook=book(root), rulebook_version=1, constraints=CONSTRAINTS)

    async def main():
        task = asyncio.ensure_future(run.execute())
        while not OBSERVED.get("started"):
            await asyncio.sleep(0.01)
        t0 = time.monotonic()
        run.cancel()
        await task
        return t0

    t0 = asyncio.run(main())
    assert run.status == RunStatus.CANCELLED
    time.sleep(0.2)
    assert OBSERVED["stopped_at"] - t0 < 1.0                                           # the thread noticed and quit


def test_blocking_work_stops_when_the_task_itself_is_cancelled(registry, root):
    defn = definition([node("w", "test.blocking")], [edge("start", "payload", "w", "start")])
    run = Run(defn, registry, rulebook=book(root), rulebook_version=1, constraints=CONSTRAINTS)

    async def main():
        task = asyncio.ensure_future(run.execute())
        while not OBSERVED.get("started"):
            await asyncio.sleep(0.01)
        t0 = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return t0

    t0 = asyncio.run(main())
    time.sleep(0.3)
    assert OBSERVED["stopped_at"] - t0 < 1.0                                           # Ctrl+C leaves no thread working on


# ---- big results ----------------------------------------------------------------------------
def test_big_outputs_are_stored_as_an_honest_preview_but_flow_whole_through_the_run(root, registry, tmp_path):
    many = {f"f{n:05}.txt": f"content {n}" for n in range(2500)}
    make_files(root, many)
    defn = definition([node("scan", "files.scan_folder", folder=str(root)), node("f", "files.filter", extensions=[".txt"])],
                      [edge("start", "payload", "scan", "start"), edge("scan", "items", "f", "items")])
    storage = Storage.open(tmp_path / "big.db")
    run = Run(defn, registry, rulebook=book(root), rulebook_version=1, constraints=CONSTRAINTS)
    storage.attach(run)
    asyncio.run(run.execute())
    assert run.status == RunStatus.COMPLETED
    assert len(run.nodes["scan"].outputs["items"]) == 2500 and run.nodes["f"].outputs["stats"]["matched"] == 2500   # whole in memory
    saved = {n["node_id"]: n for n in storage.get_run(run.run_id)["nodes"]}
    stored_items = saved["scan"]["outputs"]["items"]
    assert stored_items["_truncated"] is True and stored_items["length"] == 2500 and len(stored_items["preview"]) == 20
    assert saved["scan"]["outputs"]["stats"]["files"] == 2500                          # small ports are kept whole
    storage.close()


def test_stored_outputs_rules():
    small = {"items": [1, 2, 3], "stats": {"n": 3}}
    assert stored_outputs(small) is small
    big = {"items": [{"path": "x" * 100}] * 5000, "stats": {"n": 5000}, "blob": "q" * 400_000, "table": {"k%d" % i: i for i in range(50_000)}}
    out = stored_outputs(big)
    assert out["stats"] == {"n": 5000}
    assert out["items"]["_truncated"] and out["items"]["length"] == 5000 and out["items"]["bytes"] > 0
    assert out["blob"]["_truncated"] and len(out["blob"]["preview"]) == 1000
    assert out["table"]["_truncated"] and len(out["table"]["keys"]) == 100
    assert len(json.dumps(out)) < MAX_STORED_OUTPUT_BYTES
    assert stored_outputs({}) == {}
