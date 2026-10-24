import asyncio
import threading

import pytest
from pydantic import BaseModel

from botfoundry.storage import Storage, StorageError
from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.definition import WorkflowDefinition
from botfoundry.workflow.events import EventType
from botfoundry.workflow.node import Category, NodeError, NodeResult, NodeSpec
from botfoundry.workflow.runner import NodeStatus, Run, RunStatus
from botfoundry.workflow.types import PortSpec


class Empty(BaseModel):
    pass


class Sleep(BaseModel):
    seconds: float = 5.0


async def h_fail(ctx, cfg, inputs):
    raise NodeError("test.expected_failure", "Expected.", {"k": 1})


async def h_slow(ctx, cfg, inputs):
    await asyncio.sleep(cfg.seconds)
    return NodeResult(outputs={"result": 1})


async def h_path(ctx, cfg, inputs):
    from pathlib import PurePosixPath
    return NodeResult(outputs={"result": PurePosixPath("/tmp/x")})   # not JSON-serializable on its own


def make_spec(node_id, handler, config=Empty):
    return NodeSpec(id=node_id, version=1, title=node_id, description="", category=Category.DATA,
                    config_model=config, inputs=(PortSpec(name="value"),), outputs=(PortSpec(name="result"),),
                    handler=handler, timeout_seconds=5)


@pytest.fixture()
def registry():
    r = build_default_registry()
    r.register(make_spec("test.fail", h_fail))
    r.register(make_spec("test.slow", h_slow, Sleep))
    r.register(make_spec("test.path", h_path))
    return r


@pytest.fixture()
def storage():
    s = Storage.open(":memory:")
    yield s
    s.close()


def wf(nodes, edges=(), workflow_id="w1", name="Workflow"):
    return WorkflowDefinition.model_validate({"id": workflow_id, "name": name, "nodes": list(nodes),
                                              "edges": list(edges)})


TRIGGER = {"id": "start", "type": "core.manual_trigger", "config": {"payload": {"name": "bot"}}}


def linear(node_type="core.transform", **config):
    config = config or {"operation": "to_string"}
    return wf([TRIGGER, {"id": "x", "type": node_type, "config": config}],
              [{"source": "start", "source_port": "payload", "target": "x", "target_port": "value"}])


def run_persisted(storage, registry, definition):
    run = Run(definition, registry)
    persister = storage.attach(run)
    asyncio.run(run.execute())
    return run, persister


# ---- workflows ----------------------------------------------------------------------------
def test_workflow_round_trip(storage):
    definition = linear()
    summary = storage.save_workflow(definition)
    assert summary["node_count"] == 2 and summary["name"] == "Workflow"
    assert storage.get_workflow("w1") == definition


def test_saving_again_updates_and_keeps_created_at(storage):
    first = storage.save_workflow(linear())
    renamed = wf(linear().model_dump()["nodes"], [e for e in linear().model_dump()["edges"]], name="Renamed")
    second = storage.save_workflow(renamed)
    assert second["name"] == "Renamed"
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]
    assert len(storage.list_workflows()) == 1


def test_invalid_but_well_formed_workflow_can_be_saved(storage, registry):
    from botfoundry.workflow.validation import validate_workflow
    draft = wf([{"id": "lonely", "type": "core.transform", "config": {}}])     # no trigger, bad config
    assert not validate_workflow(draft, registry).ok
    storage.save_workflow(draft)                                                # work in progress is kept
    assert storage.get_workflow("w1") == draft
    assert storage.get_workflow("missing") is None


def test_list_and_delete_workflows(storage):
    storage.save_workflow(linear())
    storage.save_workflow(wf([TRIGGER], workflow_id="w2", name="Second"))
    assert {w["id"] for w in storage.list_workflows()} == {"w1", "w2"}
    assert storage.delete_workflow("w1") is True
    assert storage.delete_workflow("w1") is False
    assert [w["id"] for w in storage.list_workflows()] == ["w2"]


# ---- runs ---------------------------------------------------------------------------------
def test_completed_run_is_fully_persisted(storage, registry):
    run, persister = run_persisted(storage, registry, linear())
    assert persister.errors == []
    saved = storage.get_run(run.run_id)
    assert saved["status"] == "completed" and saved["workflow_id"] == "w1" and saved["finished_at"]
    assert [(n["node_id"], n["status"]) for n in saved["nodes"]] == [("start", "completed"), ("x", "completed")]
    assert saved["nodes"][1]["outputs"] == {"result": '{"name": "bot"}'}
    assert saved["nodes"][0]["started_at"] and saved["nodes"][0]["finished_at"]


def test_persisted_events_match_the_in_memory_log_exactly(storage, registry):
    run, _ = run_persisted(storage, registry, linear())
    stored = storage.get_events(run.run_id)
    assert [e.model_dump() for e in stored] == [e.model_dump() for e in run.events.all()]
    assert [e.id for e in stored] == list(range(1, len(stored) + 1))


def test_event_cursor_and_limit(storage, registry):
    run, _ = run_persisted(storage, registry, linear())
    assert [e.id for e in storage.get_events(run.run_id, after_id=5)] == [6, 7, 8]
    assert [e.id for e in storage.get_events(run.run_id, after_id=0, limit=3)] == [1, 2, 3]
    assert storage.get_events(run.run_id, after_id=8) == []
    assert storage.get_events("nope") == []


def test_failed_run_keeps_errors_and_skipped_nodes(storage, registry):
    definition = wf([TRIGGER, {"id": "bad", "type": "test.fail"}, {"id": "later", "type": "core.transform",
                    "config": {"operation": "to_string"}}],
                    [{"source": "start", "source_port": "payload", "target": "bad", "target_port": "value"},
                     {"source": "bad", "source_port": "result", "target": "later", "target_port": "value"}])
    run, _ = run_persisted(storage, registry, definition)
    saved = storage.get_run(run.run_id)
    nodes = {n["node_id"]: n for n in saved["nodes"]}
    assert saved["status"] == "failed" and saved["error"]["code"] == "test.expected_failure"
    assert nodes["bad"]["status"] == "failed" and nodes["bad"]["error"]["details"] == {"k": 1}
    assert nodes["later"]["status"] == "skipped" and "bad" in nodes["later"]["reason"]


def test_invalid_workflow_run_is_recorded_too(storage, registry):
    run, _ = run_persisted(storage, registry, wf([{"id": "x", "type": "core.transform",
                                                   "config": {"operation": "to_string"}}]))
    saved = storage.get_run(run.run_id)
    assert saved["status"] == "failed" and saved["error"]["code"] == "validation_failed"
    types = [e.type for e in storage.get_events(run.run_id)]
    assert EventType.VALIDATION_FAILED in types and types[-1] == EventType.RUN_FAILED


def test_cancelled_run_is_persisted(storage, registry):
    definition = wf([TRIGGER, {"id": "slow", "type": "test.slow", "config": {"seconds": 10}},
                     {"id": "after", "type": "core.transform", "config": {"operation": "to_string"}}],
                    [{"source": "start", "source_port": "payload", "target": "slow", "target_port": "value"},
                     {"source": "slow", "source_port": "result", "target": "after", "target_port": "value"}])
    run = Run(definition, registry)
    storage.attach(run)

    async def main():
        task = asyncio.ensure_future(run.execute())
        while run.nodes["slow"].status != NodeStatus.RUNNING:
            await asyncio.sleep(0.01)
        run.cancel()
        await task

    asyncio.run(main())
    saved = storage.get_run(run.run_id)
    assert saved["status"] == "cancelled"
    assert {n["node_id"]: n["status"] for n in saved["nodes"]} == {
        "start": "completed", "slow": "cancelled", "after": "cancelled"}
    assert storage.get_events(run.run_id)[-1].type == EventType.RUN_CANCELLED


def test_run_keeps_its_own_workflow_snapshot(storage, registry):
    run, _ = run_persisted(storage, registry, linear())
    storage.save_workflow(linear())
    storage.delete_workflow("w1")                                 # editing/deleting the workflow later...
    assert storage.get_run_workflow(run.run_id) == linear()       # ...never changes what the run executed
    assert storage.get_run(run.run_id)["status"] == "completed"


def test_list_runs_newest_first_with_filters_and_paging(storage, registry):
    runs = [run_persisted(storage, registry, linear())[0] for _ in range(3)]
    other, _ = run_persisted(storage, registry, wf([TRIGGER], workflow_id="w2", name="Other"))
    everything = storage.list_runs()
    assert len(everything) == 4 and everything[0]["run_id"] == other.run_id
    assert len(storage.list_runs(workflow_id="w1")) == 3
    assert len(storage.list_runs(limit=2)) == 2 and len(storage.list_runs(limit=2, offset=3)) == 1
    assert {r["run_id"] for r in storage.list_runs(workflow_id="w1")} == {r.run_id for r in runs}


def test_unserializable_output_does_not_lose_the_run(storage, registry):
    run, persister = run_persisted(storage, registry, linear("test.path"))
    assert persister.errors == []
    assert storage.get_run(run.run_id)["nodes"][1]["outputs"] == {"result": "/tmp/x"}


def test_a_database_failure_never_breaks_the_run(storage, registry):
    run = Run(linear(), registry)
    persister = storage.attach(run)
    storage.close()                                               # database disappears mid-run
    asyncio.run(run.execute())
    assert run.status == RunStatus.COMPLETED                      # the run itself is unaffected...
    assert persister.errors and "Could not save event" in persister.errors[0]   # ...but the problem is visible


def test_concurrent_event_writers_do_not_lose_or_duplicate_events(storage, registry):
    run = Run(linear(), registry)
    storage.attach(run)

    def worker():
        for i in range(50):
            run.events.append(EventType.NODE_PROGRESS, "x", {"fraction": i / 50, "message": "tick"})

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    stored = storage.get_events(run.run_id, limit=5000)
    assert len(stored) == 1 + 200                                 # run_created + 4 x 50
    assert [e.id for e in stored] == list(range(1, 202))


# ---- restart safety -----------------------------------------------------------------------
def simulate_crash_mid_node(storage, registry):
    """A run that was executing node 'x' when the process died."""
    run = Run(linear(), registry)
    storage.attach(run)
    run.status = RunStatus.RUNNING
    run.nodes["start"].status = NodeStatus.COMPLETED
    run.nodes["x"].status = NodeStatus.RUNNING
    run.events.append(EventType.NODE_STARTED, "x", {"type": "core.transform", "version": 1})
    return run


def test_interrupted_runs_are_failed_not_resumed(tmp_path, registry):
    path = tmp_path / "bot.db"
    first = Storage.open(path)
    crashed = simulate_crash_mid_node(first, registry)
    done, _ = run_persisted(first, registry, linear())
    first.close()                                                 # process "dies"

    second = Storage.open(path)                                   # next launch
    assert second.mark_interrupted_runs() == [crashed.run_id]
    saved = second.get_run(crashed.run_id)
    nodes = {n["node_id"]: n for n in saved["nodes"]}
    assert saved["status"] == "failed" and saved["error"]["code"] == "run.interrupted"
    assert nodes["x"]["status"] == "failed" and nodes["x"]["error"]["code"] == "node.interrupted"
    last = second.get_events(crashed.run_id)[-1]
    assert last.type == EventType.RUN_FAILED and last.payload["interrupted"] is True
    assert last.payload["failed_node"] == "x"
    assert last.id == len(second.get_events(crashed.run_id))      # numbering continues without a gap
    assert second.get_run(done.run_id)["status"] == "completed"   # finished runs are untouched
    assert second.mark_interrupted_runs() == []                   # and it only ever happens once
    second.close()


def test_pending_nodes_of_an_interrupted_run_are_marked_skipped(tmp_path, registry):
    path = tmp_path / "bot.db"
    first = Storage.open(path)
    run = Run(linear(), registry)
    first.attach(run)                                             # created, nothing started
    first.close()
    second = Storage.open(path)
    assert second.mark_interrupted_runs() == [run.run_id]
    statuses = {n["node_id"]: (n["status"], n["reason"]) for n in second.get_run(run.run_id)["nodes"]}
    assert all(s == "skipped" and "interrupted" in r for s, r in statuses.values())


def test_history_survives_a_restart(tmp_path, registry):
    path = tmp_path / "bot.db"
    first = Storage.open(path)
    first.save_workflow(linear())
    run, _ = run_persisted(first, registry, linear())
    first.close()
    second = Storage.open(path)
    assert second.get_workflow("w1") == linear()
    assert second.get_run(run.run_id)["status"] == "completed"
    assert len(second.get_events(run.run_id)) == 8
    second.close()


def test_open_reports_a_corrupt_database(tmp_path):
    path = tmp_path / "bot.db"
    path.write_bytes(b"garbage" * 100)
    with pytest.raises(StorageError):
        Storage.open(path)


def test_task_cancellation_is_persisted_with_the_running_node_cancelled(storage, registry):
    definition = wf([TRIGGER, {"id": "slow", "type": "test.slow", "config": {"seconds": 10}}],
                    [{"source": "start", "source_port": "payload", "target": "slow", "target_port": "value"}])
    run = Run(definition, registry)
    storage.attach(run)

    async def main():
        task = asyncio.ensure_future(run.execute())
        while run.nodes["slow"].status != NodeStatus.RUNNING:
            await asyncio.sleep(0.01)
        task.cancel()                                   # what Ctrl+C does to the CLI's run
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    saved = storage.get_run(run.run_id)
    assert saved["status"] == "cancelled"
    assert {n["node_id"]: n["status"] for n in saved["nodes"]} == {"start": "completed", "slow": "cancelled"}
    assert storage.get_events(run.run_id)[-1].payload["running_node"] == "slow"


def test_runs_created_within_the_same_clock_tick_still_list_newest_first(storage, registry):
    """Windows' clock advances only about every 15 ms, so quick successive runs can share a timestamp."""
    from datetime import datetime, timezone
    frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ids = []
    for _ in range(5):
        run = Run(linear(), registry)
        run.created_at = frozen                      # identical timestamps, as on a coarse clock
        storage.attach(run)
        ids.append(run.run_id)
    assert [r["run_id"] for r in storage.list_runs()] == list(reversed(ids))
    assert [r["run_id"] for r in storage.list_runs(limit=2)] == [ids[4], ids[3]]
    assert [r["run_id"] for r in storage.list_runs(limit=2, offset=2)] == [ids[2], ids[1]]
