import asyncio
import sqlite3
import time

import pytest
from pydantic import BaseModel

from botfoundry.rules.constraints import SystemConstraints
from botfoundry.rules.models import Rulebook
from botfoundry.rules.paths import WINDOWS
from botfoundry.storage import Storage
from botfoundry.workflow.approvals import ApprovalNotFound, ApprovalNotPending
from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.definition import WorkflowDefinition
from botfoundry.workflow.events import TERMINAL_EVENTS, EventType
from botfoundry.workflow.node import Category, NodeResult, NodeSpec
from botfoundry.workflow.runner import NodeStatus, Run, RunStatus
from botfoundry.workflow.types import PortSpec

DL = "C:\\Users\\Me\\Downloads"
CONSTRAINTS = SystemConstraints(protected_paths=("C:\\Windows",), too_broad_paths=("C:\\Users\\Me",), style=WINDOWS)
CALLS: list[str] = []


class FileOp(BaseModel):
    permission: str = "fs.move"
    paths: list[str] = []
    files: int = 0


class Work(BaseModel):
    seconds: float = 0.0
    ask_first: bool = False
    note: str = "needs a person"


async def h_fileop(ctx, cfg, inputs):
    resolved = ctx.authorize(cfg.permission, cfg.paths)
    ctx.budget.consume_files(cfg.files)
    CALLS.append(ctx.node_id)
    return NodeResult(outputs={"result": resolved})


async def h_work(ctx, cfg, inputs):
    CALLS.append(ctx.node_id)
    if cfg.ask_first:
        await ctx.request_approval(cfg.note, {"files": 3})
    await asyncio.sleep(cfg.seconds)
    return NodeResult(outputs={"result": "done"})


def spec(node_id, handler, permissions=(), config=FileOp, timeout=5.0):
    return NodeSpec(id=node_id, version=1, title=node_id, description="", category=Category.FILES,
                    config_model=config, inputs=(PortSpec(name="value"),), outputs=(PortSpec(name="result"),),
                    handler=handler, permissions=frozenset(permissions), timeout_seconds=timeout)


@pytest.fixture()
def registry():
    CALLS.clear()
    r = build_default_registry()
    r.register(spec("test.mover", h_fileop, {"fs.move"}))
    r.register(spec("test.deleter", h_fileop, {"fs.delete"}))
    r.register(spec("test.reader", h_fileop, {"fs.read"}))
    r.register(spec("test.worker", h_work, {"fs.move"}, Work, timeout=0.4))
    r.register(spec("test.free_worker", h_work, (), Work, timeout=0.4))
    return r


def book(**changes):
    data = {"bot_id": "fixer", "name": "Fixer", "allowed_folders": [DL], "forbidden_folders": [DL + "\\Keep"],
            "permissions": {"fs.read": "allow", "fs.move": "allow"}, "limits": {"max_files_per_run": 100}}
    data.update(changes)
    return Rulebook.model_validate(data)


def wf(*nodes, bot_id="fixer"):
    ids = ["start"] + [n["id"] for n in nodes]
    edges = [{"source": a, "source_port": "payload" if a == "start" else "result", "target": b, "target_port": "value"}
             for a, b in zip(ids, ids[1:])]
    return WorkflowDefinition.model_validate({
        "id": "w", "name": "W", "bot_id": bot_id,
        "nodes": [{"id": "start", "type": "core.manual_trigger", "config": {"payload": {"a": 1}}}, *nodes],
        "edges": edges})


def node(node_id, node_type, **config):
    return {"id": node_id, "type": node_type, "config": config}


def make_run(definition, registry, rulebook=None, **kwargs):
    return Run(definition, registry, rulebook=rulebook, rulebook_version=1 if rulebook else None,
               constraints=CONSTRAINTS, **kwargs)


def go(run):
    asyncio.run(run.execute())
    return run


def types(run):
    return [e.type for e in run.events.all()]


def node_status(run):
    return {k: v.status for k, v in run.nodes.items()}


async def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.01)


def auto_answer(run, approve=True, note=None):
    run.events.subscribe(lambda e: run.respond(e.payload["approval_id"], approve, note)
                         if e.type == EventType.APPROVAL_REQUESTED else None)


def drive(run, scenario):
    """Run `scenario(run)` alongside the run and return when both are done."""
    async def main():
        task = asyncio.ensure_future(run.execute())
        try:
            await scenario(run)
        finally:
            if not task.done():
                run.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(main())
    return run


# ---- a Rulebook is required for permissioned nodes -----------------------------------------
def test_permissioned_nodes_need_a_bot_and_a_rulebook(registry):
    no_bot = go(make_run(wf(node("m", "test.mover", paths=[DL + "\\a"]), bot_id=None), registry))
    assert no_bot.status == RunStatus.FAILED and no_bot.error["code"] == "validation_failed"
    issue = next(i for i in no_bot.events.all()[2].payload["issues"] if i["code"] == "rulebook.required")
    assert "no bot_id" in issue["message"] and "fs.move" in issue["message"]
    no_book = go(make_run(wf(node("m", "test.mover", paths=[DL + "\\a"])), registry))
    assert "has no Rulebook" in next(i for i in no_book.events.all()[2].payload["issues"]
                                      if i["code"] == "rulebook.required")["message"]
    assert CALLS == [] and node_status(no_book)["m"] == NodeStatus.PENDING


def test_workflows_without_permissions_still_run_without_a_rulebook(registry):
    run = go(make_run(wf(node("w", "test.free_worker"), bot_id=None), registry))
    assert run.status == RunStatus.COMPLETED


# ---- enforcement before a node runs ---------------------------------------------------------
def test_allowed_permission_runs_and_returns_resolved_paths(registry):
    run = go(make_run(wf(node("m", "test.mover", paths=[DL + "\\a.txt", "c:/users/me/downloads/sub/b.txt"])),
                      registry, book()))
    assert run.status == RunStatus.COMPLETED and CALLS == ["m"]
    # Windows reports the real capitalization of folders that exist on disk, so compare ignoring case.
    resolved = [p.lower() for p in run.nodes["m"].outputs["result"]]
    assert resolved == [(DL + "\\a.txt").lower(), "c:\\users\\me\\downloads\\sub\\b.txt"]


@pytest.mark.parametrize("permissions,default,reason", [
    ({"fs.move": "deny"}, "deny", "rulebook.denied"),
    ({}, "deny", "rulebook.not_granted"),
])
def test_denied_permission_stops_the_node_before_any_code_runs(registry, permissions, default, reason):
    definition = wf(node("m", "test.mover", paths=[DL + "\\a"]), node("after", "core.transform", operation="to_string"))
    run = go(make_run(definition, registry, book(permissions=permissions, default_policy=default)))
    assert run.status == RunStatus.FAILED and CALLS == []
    error = run.nodes["m"].error
    assert error["code"] == "permission.denied" and error["details"] == {"permission": "fs.move", "reason": reason}
    assert node_status(run)["after"] == NodeStatus.SKIPPED


def test_system_limits_beat_a_rulebook_that_was_never_validated(registry):
    risky = book(permissions={"fs.delete": "allow"})                 # would be refused at save time
    run = go(make_run(wf(node("d", "test.deleter", permission="fs.delete", paths=[DL + "\\a"])), registry, risky))
    assert run.status == RunStatus.FAILED and CALLS == []
    assert run.nodes["d"].error["details"]["reason"] == "system.limit"


def test_paths_are_checked_when_the_node_asks(registry):
    run = go(make_run(wf(node("m", "test.mover", paths=[DL + "\\ok.txt", "C:\\Users\\Me\\Documents\\x", DL + "\\Keep\\y"])),
                      registry, book()))
    error = run.nodes["m"].error
    assert run.status == RunStatus.FAILED and error["code"] == "path.outside_allowed" and CALLS == []
    refused = {p["path"]: p["code"] for p in error["details"]["refused_paths"]}
    assert refused == {"C:\\Users\\Me\\Documents\\x": "path.outside_allowed", DL + "\\Keep\\y": "path.forbidden"}


def test_a_node_cannot_use_a_permission_it_did_not_declare(registry):
    run = go(make_run(wf(node("r", "test.reader", permission="fs.move", paths=[DL + "\\a"])), registry, book()))
    assert run.nodes["r"].error["code"] == "permission.undeclared" and CALLS == []


def test_the_file_budget_is_shared_across_the_whole_run(registry):
    definition = wf(node("a", "test.mover", paths=[DL + "\\1"], files=3), node("b", "test.mover", paths=[DL + "\\2"], files=3))
    run = go(make_run(definition, registry, book(limits={"max_files_per_run": 5})))
    assert node_status(run)["a"] == NodeStatus.COMPLETED and node_status(run)["b"] == NodeStatus.FAILED
    error = run.nodes["b"].error
    assert error["code"] == "limit.max_files_per_run" and error["details"] == {"limit": 5, "used": 3, "requested": 3}
    assert run.budget.used_files == 3                                  # the refused 3 were never counted


def test_a_handler_without_a_run_context_is_denied_everything():
    from botfoundry.workflow.node import CancelToken, NodeContext, NodeError
    ctx = NodeContext(run_id="r", node_id="n", cancel=CancelToken())
    with pytest.raises(NodeError) as info:
        ctx.authorize("fs.read", [DL])
    assert info.value.code == "permission.denied"


def test_the_run_records_which_rulebook_it_followed(registry):
    run = go(make_run(wf(node("m", "test.mover", paths=[DL + "\\a"])), registry, book()))
    assert run.events.all()[0].payload["rulebook"] == {"bot_id": "fixer", "version": 1}
    assert run.snapshot()["rulebook"] == {"bot_id": "fixer", "version": 1} and run.rulebook_version == 1
    plain = go(make_run(wf(node("w", "test.free_worker"), bot_id=None), registry))
    assert plain.events.all()[0].payload["rulebook"] is None


# ---- approvals ------------------------------------------------------------------------------
ASK = {"fs.move": "ask"}


def ask_run(registry, **kwargs):
    return make_run(wf(node("m", "test.mover", paths=[DL + "\\a.txt"]), node("after", "core.transform", operation="to_string")),
                    registry, book(permissions=ASK), **kwargs)


def test_ask_pauses_the_run_until_a_person_approves(registry):
    run = ask_run(registry)
    seen = {}

    async def scenario(r):
        await until(lambda: r.status == RunStatus.WAITING_APPROVAL)
        seen["node"], seen["pending"], seen["calls"] = r.nodes["m"].status, r.pending_approvals(), list(CALLS)
        r.respond(seen["pending"][0].approval_id, True, "looks fine")
        await until(lambda: r.is_finished)

    drive(run, scenario)
    assert seen["node"] == NodeStatus.WAITING_APPROVAL and seen["calls"] == []     # nothing ran while waiting
    pending = seen["pending"][0]
    assert pending.kind == "node_permissions" and pending.node_id == "m" and "fs.move" in pending.summary
    assert run.status == RunStatus.COMPLETED and CALLS == ["m"]
    assert [e.type for e in run.events.all() if e.node_id == "m"] == [
        EventType.NODE_STARTED, EventType.APPROVAL_REQUESTED, EventType.APPROVAL_GRANTED, EventType.NODE_COMPLETED]
    granted = next(e for e in run.events.all() if e.type == EventType.APPROVAL_GRANTED)
    assert granted.payload == {"approval_id": "ap-1", "decided_by": "user", "reason": "approved", "note": "looks fine"}


def test_a_denial_fails_the_node_and_nothing_runs(registry):
    run = ask_run(registry)
    auto_answer(run, approve=False, note="not now")
    go(run)
    assert run.status == RunStatus.FAILED and CALLS == []
    error = run.nodes["m"].error
    assert error["code"] == "node.approval_denied" and error["details"]["reason"] == "denied"
    assert error["details"]["note"] == "not now" and "not now" in error["message"]
    assert node_status(run)["after"] == NodeStatus.SKIPPED


def test_no_answer_counts_as_a_denial(registry):
    run = go(ask_run(registry, approval_timeout=0.3))
    assert run.status == RunStatus.FAILED and CALLS == []
    assert run.nodes["m"].error["details"]["reason"] == "timeout" and "nobody answered" in run.nodes["m"].error["message"]
    denied = next(e for e in run.events.all() if e.type == EventType.APPROVAL_DENIED)
    assert denied.payload["decided_by"] == "engine" and denied.payload["reason"] == "timeout"


def test_a_persons_note_can_never_pose_as_an_engine_outcome(registry):
    run = ask_run(registry)
    auto_answer(run, approve=False, note="cancelled")              # tries to look like "the run was cancelled"
    go(run)
    assert run.status == RunStatus.FAILED                          # a denial, NOT a cancellation
    assert run.nodes["m"].error["details"]["reason"] == "denied"


def test_cancelling_while_waiting_closes_the_question_and_the_node(registry):
    run = ask_run(registry)

    async def scenario(r):
        await until(lambda: r.status == RunStatus.WAITING_APPROVAL)
        r.cancel()
        await until(lambda: r.is_finished)

    drive(run, scenario)
    assert run.status == RunStatus.CANCELLED and CALLS == []
    assert node_status(run)["m"] == NodeStatus.CANCELLED and not run.pending_approvals()
    assert run.events.all()[-1].type == EventType.RUN_CANCELLED
    order = types(run)
    assert order.index(EventType.APPROVAL_DENIED) < order.index(EventType.RUN_CANCELLED)
    assert next(e for e in run.events.all() if e.type == EventType.APPROVAL_DENIED).payload["reason"] == "cancelled"
    assert [t for t in order if t in TERMINAL_EVENTS] == [EventType.RUN_CANCELLED]


def test_cancelling_the_task_while_waiting_leaves_nothing_open(registry):
    run = ask_run(registry)

    async def main():
        task = asyncio.ensure_future(run.execute())
        await until(lambda: run.status == RunStatus.WAITING_APPROVAL)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert run.status == RunStatus.CANCELLED and not run.pending_approvals()
    assert node_status(run)["m"] == NodeStatus.CANCELLED


def test_each_node_asks_separately(registry):
    definition = wf(node("a", "test.mover", paths=[DL + "\\1"]), node("b", "test.mover", paths=[DL + "\\2"]))
    run = make_run(definition, registry, book(permissions=ASK))
    auto_answer(run)
    go(run)
    assert run.status == RunStatus.COMPLETED
    assert [e.payload["approval_id"] for e in run.events.all() if e.type == EventType.APPROVAL_REQUESTED] == ["ap-1", "ap-2"]


def test_approving_does_not_widen_what_the_paths_check_allows(registry):
    definition = wf(node("m", "test.mover", paths=["C:\\Users\\Me\\Documents\\x"]))
    run = make_run(definition, registry, book(permissions=ASK))
    auto_answer(run)
    go(run)
    assert run.status == RunStatus.FAILED and run.nodes["m"].error["code"] == "path.outside_allowed"


def test_answering_errors(registry):
    run = ask_run(registry)
    results = {}

    async def scenario(r):
        await until(lambda: r.status == RunStatus.WAITING_APPROVAL)
        with pytest.raises(ApprovalNotFound):
            r.respond("ap-99", True)
        r.respond("ap-1", True)
        with pytest.raises(ApprovalNotPending):
            r.respond("ap-1", False)                               # the first answer stands
        await until(lambda: r.is_finished)
        results["status"] = r.status

    drive(run, scenario)
    assert results["status"] == RunStatus.COMPLETED
    with pytest.raises(ApprovalNotPending):
        run.respond("ap-1", True)                                  # and it stays closed after the run ends


def test_answering_from_another_thread(registry):
    import threading
    run = ask_run(registry)

    async def scenario(r):
        await until(lambda: r.status == RunStatus.WAITING_APPROVAL)
        threading.Thread(target=lambda: r.respond("ap-1", True)).start()
        await until(lambda: r.is_finished)

    drive(run, scenario)
    assert run.status == RunStatus.COMPLETED


# ---- explicit approvals from inside a node, and the clocks -----------------------------------
def worker_run(registry, **config):
    return make_run(wf(node("w", "test.worker", **config)), registry, book(permissions={"fs.move": "allow"}))


def test_a_node_can_ask_for_approval_itself(registry):
    run = worker_run(registry, ask_first=True, note="Move 3 files?")
    seen = {}

    async def scenario(r):
        await until(lambda: r.pending_approvals())
        approval = r.pending_approvals()[0]
        seen.update(kind=approval.kind, summary=approval.summary, details=approval.details, node=r.nodes["w"].status)
        r.respond(approval.approval_id, True)
        await until(lambda: r.is_finished)

    drive(run, scenario)
    assert seen == {"kind": "explicit", "summary": "Move 3 files?", "details": {"files": 3},
                    "node": NodeStatus.WAITING_APPROVAL}
    assert run.status == RunStatus.COMPLETED


def test_waiting_for_a_person_does_not_count_against_the_node_timeout(registry):
    run = worker_run(registry, ask_first=True, seconds=0.1)       # node timeout is 0.4s

    async def scenario(r):
        await until(lambda: r.pending_approvals())
        await asyncio.sleep(1.0)                                   # a slow human: 2.5x the node's timeout
        r.respond("ap-1", True)
        await until(lambda: r.is_finished)

    drive(run, scenario)
    assert run.status == RunStatus.COMPLETED


def test_work_after_the_approval_still_has_to_finish_in_time(registry):
    run = worker_run(registry, ask_first=True, seconds=2.0)
    auto_answer(run)
    go(run)
    assert run.status == RunStatus.FAILED and run.nodes["w"].error["code"] == "node.timeout"


def test_waiting_for_a_person_does_not_count_against_the_run_limit(registry):
    limited = make_run(wf(node("w", "test.worker", ask_first=True, seconds=0.1)), registry,
                       book(permissions={"fs.move": "allow"}, limits={"max_run_seconds": 0.6}))

    async def scenario(r):
        await until(lambda: r.pending_approvals())
        await asyncio.sleep(1.2)                                   # twice the run limit
        r.respond("ap-1", True)
        await until(lambda: r.is_finished)

    drive(limited, scenario)
    assert limited.status == RunStatus.COMPLETED


def test_the_run_time_limit_stops_a_node_that_works_too_long(registry):
    r = registry
    r.register(spec("test.long", h_work, {"fs.move"}, Work, timeout=30))
    run = make_run(wf(node("w", "test.long", seconds=5)), r,
                   book(permissions={"fs.move": "allow"}, limits={"max_run_seconds": 0.3}))
    started = time.monotonic()
    go(run)
    assert time.monotonic() - started < 2
    assert run.status == RunStatus.FAILED and run.nodes["w"].error["code"] == "run.time_limit"
    assert run.nodes["w"].error["details"] == {"max_run_seconds": 0.3}


def test_a_denied_explicit_approval_fails_the_node(registry):
    run = worker_run(registry, ask_first=True)
    auto_answer(run, approve=False)
    go(run)
    assert run.status == RunStatus.FAILED and run.nodes["w"].error["code"] == "node.approval_denied"


# ---- the built-in approval gate -------------------------------------------------------------
def gate_workflow(payload=None, summary="Continue?"):
    return WorkflowDefinition.model_validate({"id": "g", "name": "G", "nodes": [
        {"id": "start", "type": "core.manual_trigger", "config": {"payload": payload or {"n": 1}}},
        {"id": "gate", "type": "core.approval_gate", "config": {"summary": summary, "details": {"why": "test"}}},
        {"id": "end", "type": "core.transform", "config": {"operation": "to_string"}}], "edges": [
        {"source": "start", "source_port": "payload", "target": "gate", "target_port": "value"},
        {"source": "gate", "source_port": "result", "target": "end", "target_port": "value"}]})


def test_approval_gate_passes_the_value_on_when_approved(registry):
    run = Run(gate_workflow(), registry, constraints=CONSTRAINTS)
    auto_answer(run)
    go(run)
    assert run.status == RunStatus.COMPLETED and run.nodes["gate"].outputs == {"result": {"n": 1}}
    requested = next(e for e in run.events.all() if e.type == EventType.APPROVAL_REQUESTED)
    assert requested.payload["kind"] == "explicit" and requested.payload["summary"] == "Continue?"
    assert requested.payload["details"] == {"why": "test", "value_preview": '{"n": 1}'}


def test_approval_gate_stops_the_run_when_denied(registry):
    run = Run(gate_workflow(), registry, constraints=CONSTRAINTS)
    auto_answer(run, approve=False)
    go(run)
    assert run.status == RunStatus.FAILED and node_status(run)["end"] == NodeStatus.SKIPPED


def test_approval_gate_previews_are_truncated(registry):
    run = Run(gate_workflow(payload={"blob": "x" * 5000}), registry, constraints=CONSTRAINTS)
    auto_answer(run)
    go(run)
    preview = next(e for e in run.events.all() if e.type == EventType.APPROVAL_REQUESTED).payload["details"]["value_preview"]
    assert len(preview) == 503 and preview.endswith("...")


# ---- persistence of all of this -------------------------------------------------------------
def test_waiting_state_and_approvals_are_persisted_as_they_happen(registry, tmp_path):
    storage = Storage.open(tmp_path / "e.db")
    run = ask_run(registry)
    storage.attach(run)
    seen = {}

    async def scenario(r):
        await until(lambda: r.status == RunStatus.WAITING_APPROVAL)
        saved = storage.get_run(r.run_id)
        seen["run"] = saved["status"]
        seen["node"] = {n["node_id"]: n["status"] for n in saved["nodes"]}["m"]
        seen["pending"] = [a["approval_id"] for a in storage.get_approvals(r.run_id) if a["status"] == "pending"]
        r.respond("ap-1", True, "ok")
        await until(lambda: r.is_finished)

    drive(run, scenario)
    assert seen == {"run": "waiting_approval", "node": "waiting_approval", "pending": ["ap-1"]}
    final = storage.get_approvals(run.run_id)
    assert [(a["status"], a["reason"], a["note"], a["kind"]) for a in final] == [("granted", "approved", "ok", "node_permissions")]
    saved = storage.get_run(run.run_id)
    assert saved["status"] == "completed" and saved["rulebook_bot_id"] == "fixer" and saved["rulebook_version"] == 1
    assert storage.list_runs()[0]["rulebook_bot_id"] == "fixer"
    storage.close()


def test_a_crash_while_waiting_fails_the_run_and_closes_the_question(registry, tmp_path):
    live = Storage.open(tmp_path / "live.db")
    run = ask_run(registry)
    live.attach(run)
    crashed = tmp_path / "crashed.db"

    async def scenario(r):
        await until(lambda: r.status == RunStatus.WAITING_APPROVAL)
        copy = sqlite3.connect(crashed)                           # the database exactly as a crash would leave it
        live.db._conn.backup(copy)
        copy.close()
        r.cancel()
        await until(lambda: r.is_finished)

    drive(run, scenario)
    live.close()

    after = Storage.open(crashed)
    assert after.get_run(run.run_id)["status"] == "waiting_approval"
    assert after.mark_interrupted_runs() == [run.run_id]
    saved = after.get_run(run.run_id)
    assert saved["status"] == "failed" and saved["error"]["code"] == "run.interrupted"
    assert {n["node_id"]: n["status"] for n in saved["nodes"]}["m"] == "failed"
    approval = after.get_approvals(run.run_id)[0]
    assert approval["status"] == "denied" and approval["reason"] == "interrupted"
    events = after.get_events(run.run_id)
    assert [e.id for e in events] == list(range(1, len(events) + 1))
    assert [e.type for e in events][-2:] == [EventType.APPROVAL_DENIED, EventType.RUN_FAILED]
    assert after.mark_interrupted_runs() == []
    after.close()
