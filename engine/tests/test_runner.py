import asyncio
import threading
import time

import pytest
from pydantic import BaseModel

from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.definition import WorkflowDefinition
from botfoundry.workflow.events import TERMINAL_EVENTS, EventType
from botfoundry.workflow.node import Category, NodeError, NodeResult, NodeSpec
from botfoundry.workflow.runner import NodeStatus, Run, RunStatus, run_workflow
from botfoundry.workflow.types import DataType, PortSpec, matches_type


class Empty(BaseModel):
    pass


class SleepConfig(BaseModel):
    seconds: float = 5.0


CALLS: list[str] = []          # which test nodes actually executed
FLAGS: dict[str, bool] = {}    # side-channel observations from handlers


def spec(node_id, handler, *, inputs=(PortSpec(name="value"),), outputs=(PortSpec(name="result"),),
         config=Empty, timeout=2.0, cancellable=True, category=Category.DATA):
    return NodeSpec(id=node_id, version=1, title=node_id, description="", category=category, config_model=config,
                    inputs=inputs, outputs=outputs, handler=handler, timeout_seconds=timeout, cancellable=cancellable)


async def h_echo(ctx, cfg, inputs):
    CALLS.append(ctx.node_id)
    return NodeResult(outputs={"result": inputs.get("value")})


async def h_fail(ctx, cfg, inputs):
    CALLS.append(ctx.node_id)
    raise NodeError("test.expected_failure", "This failure is expected.", {"why": "test"})


async def h_boom(ctx, cfg, inputs):
    CALLS.append(ctx.node_id)
    raise RuntimeError("kaboom")


async def h_slow(ctx, cfg, inputs):
    CALLS.append(ctx.node_id)
    try:
        await asyncio.sleep(cfg.seconds)
    except asyncio.CancelledError:
        FLAGS[f"{ctx.node_id}.interrupted"] = True
        raise
    return NodeResult(outputs={"result": "slept"})


async def h_slow_uninterruptible(ctx, cfg, inputs):
    CALLS.append(ctx.node_id)
    await asyncio.sleep(cfg.seconds)
    return NodeResult(outputs={"result": "finished"})


async def h_progress(ctx, cfg, inputs):
    ctx.report_progress(0.25, "quarter")
    ctx.report_progress(2.0, "clamped")
    ctx.report_progress(None, "indeterminate")
    return NodeResult(outputs={"result": 1})


async def h_undeclared(ctx, cfg, inputs):
    return NodeResult(outputs={"nope": 1})


async def h_wrong_type(ctx, cfg, inputs):
    return NodeResult(outputs={"result": 123})   # port is STRING


async def h_not_result(ctx, cfg, inputs):
    return {"result": "x"}


async def h_optional(ctx, cfg, inputs):
    CALLS.append(ctx.node_id)
    return NodeResult(outputs={"result": sorted(inputs)})


@pytest.fixture()
def registry():
    CALLS.clear()
    FLAGS.clear()
    r = build_default_registry()
    r.register(spec("test.echo", h_echo))
    r.register(spec("test.fail", h_fail))
    r.register(spec("test.boom", h_boom))
    r.register(spec("test.slow", h_slow, config=SleepConfig, timeout=0.3))
    r.register(spec("test.slow_long", h_slow, config=SleepConfig, timeout=10))
    r.register(spec("test.slow_fixed", h_slow_uninterruptible, config=SleepConfig, timeout=10, cancellable=False))
    r.register(spec("test.progress", h_progress, inputs=()))
    r.register(spec("test.undeclared", h_undeclared))
    r.register(spec("test.wrong_type", h_wrong_type, outputs=(PortSpec(name="result", type=DataType.STRING),)))
    r.register(spec("test.not_result", h_not_result))
    r.register(spec("test.strict_text", h_echo, inputs=(PortSpec(name="value", type=DataType.STRING),)))
    r.register(spec("test.optional", h_optional,
                    inputs=(PortSpec(name="a", required=False), PortSpec(name="b", required=False))))
    return r


def wf(nodes, edges=()):
    return WorkflowDefinition.model_validate({"id": "t", "name": "T", "nodes": list(nodes), "edges": list(edges)})


def N(node_id, node_type, **config):
    return {"id": node_id, "type": node_type, "config": config}


def E(source, source_port, target, target_port="value"):
    return {"source": source, "source_port": source_port, "target": target, "target_port": target_port}


TRIGGER = N("start", "core.manual_trigger", payload={"name": "bot"})
FROM_START = E("start", "payload", "x")


def execute(definition, registry):
    return asyncio.run(run_workflow(definition, registry))


def types(run):
    return [e.type for e in run.events.all()]


def statuses(run):
    return {k: v.status for k, v in run.nodes.items()}


# ---- happy path and events ----------------------------------------------------------------
def test_linear_run_completes_with_exact_event_sequence(registry):
    run = execute(wf([TRIGGER, N("x", "test.echo")], [FROM_START]), registry)
    assert run.status == RunStatus.COMPLETED
    assert types(run) == [
        EventType.RUN_CREATED, EventType.VALIDATION_STARTED, EventType.VALIDATION_SUCCEEDED,
        EventType.NODE_STARTED, EventType.NODE_COMPLETED, EventType.NODE_STARTED, EventType.NODE_COMPLETED,
        EventType.RUN_COMPLETED]
    assert run.nodes["x"].outputs == {"result": {"name": "bot"}}   # data really flowed through


def test_event_ids_run_id_timestamps_and_single_terminal_event(registry):
    run = execute(wf([TRIGGER, N("x", "test.echo")], [FROM_START]), registry)
    events = run.events.all()
    assert [e.id for e in events] == list(range(1, len(events) + 1))
    assert {e.run_id for e in events} == {run.run_id}
    assert all(a.timestamp <= b.timestamp for a, b in zip(events, events[1:]))
    assert [e.type for e in events if e.type in TERMINAL_EVENTS] == [EventType.RUN_COMPLETED]
    assert events[-1].type in TERMINAL_EVENTS


def test_polling_with_a_cursor(registry):
    run = execute(wf([TRIGGER, N("x", "test.echo")], [FROM_START]), registry)
    assert [e.id for e in run.events.since(5)] == [6, 7, 8]
    assert run.events.since(8) == []


def test_nodes_run_in_dependency_order(registry):
    nodes = [N("c", "test.echo"), N("b", "test.echo"), N("a", "test.echo"), TRIGGER]
    edges = [E("start", "payload", "a"), E("a", "result", "b"), E("b", "result", "c")]
    execute(wf(nodes, edges), registry)
    assert CALLS == ["a", "b", "c"]


def test_runs_are_independent(registry):
    definition = wf([TRIGGER, N("x", "test.echo")], [FROM_START])
    first, second = Run(definition, registry), Run(definition, registry)
    asyncio.run(first.execute())
    assert second.status == RunStatus.CREATED and first.run_id != second.run_id
    assert len(second.events.all()) == 1


def test_a_run_cannot_be_executed_twice(registry):
    run = Run(wf([TRIGGER]), registry)
    asyncio.run(run.execute())
    with pytest.raises(RuntimeError):
        asyncio.run(run.execute())


# ---- branching ----------------------------------------------------------------------------
def branching(payload, registry):
    nodes = [N("start", "core.manual_trigger", payload=payload),
             N("check", "core.condition", operator="is_truthy"),
             N("yes", "test.echo"), N("no", "test.echo"), N("after_no", "test.echo")]
    edges = [E("start", "payload", "check"), E("check", "true", "yes"), E("check", "false", "no"),
             E("no", "result", "after_no")]
    return execute(wf(nodes, edges), registry)


def test_true_branch_runs_false_branch_skipped_with_cascade(registry):
    run = branching({"a": 1}, registry)
    assert run.status == RunStatus.COMPLETED
    assert statuses(run)["yes"] == NodeStatus.COMPLETED
    assert statuses(run)["no"] == NodeStatus.SKIPPED
    assert statuses(run)["after_no"] == NodeStatus.SKIPPED          # skip cascades downstream
    assert CALLS == ["yes"]                                          # skipped handlers never ran
    assert "inactive" in run.nodes["no"].reason
    assert types(run).count(EventType.NODE_SKIPPED) == 2


def test_false_branch_runs_true_branch_skipped(registry):
    run = branching({}, registry)                                    # empty object is falsy
    assert statuses(run)["no"] == NodeStatus.COMPLETED
    assert statuses(run)["after_no"] == NodeStatus.COMPLETED
    assert statuses(run)["yes"] == NodeStatus.SKIPPED
    assert CALLS == ["no", "after_no"]


def test_optional_inputs_may_be_inactive(registry):
    nodes = [TRIGGER, N("check", "core.condition", operator="is_truthy"), N("opt", "test.optional")]
    edges = [E("start", "payload", "check"), E("check", "true", "opt", "a"), E("check", "false", "opt", "b")]
    run = execute(wf(nodes, edges), registry)
    assert run.status == RunStatus.COMPLETED
    assert run.nodes["opt"].outputs == {"result": ["a"]}             # only the active optional input arrived


# ---- validation ---------------------------------------------------------------------------
def test_invalid_workflow_never_starts(registry):
    run = execute(wf([N("x", "test.echo")]), registry)               # no trigger, missing input
    assert run.status == RunStatus.FAILED
    assert types(run) == [EventType.RUN_CREATED, EventType.VALIDATION_STARTED,
                          EventType.VALIDATION_FAILED, EventType.RUN_FAILED]
    assert run.error["code"] == "validation_failed"
    assert CALLS == []
    issues = run.events.all()[2].payload["issues"]
    assert {i["code"] for i in issues} >= {"graph.no_trigger", "node.missing_input"}


def test_warnings_do_not_block_a_run(registry):
    nodes = [TRIGGER, N("x", "test.echo"), N("island", "test.optional")]
    run = execute(wf(nodes, [FROM_START]), registry)
    assert run.status == RunStatus.COMPLETED
    assert statuses(run)["island"] == NodeStatus.COMPLETED           # reachable-or-not, it has no required inputs
    warnings = run.events.all()[2].payload["warnings"]
    assert [w["code"] for w in warnings] == ["node.unreachable"]


# ---- failures -----------------------------------------------------------------------------
def test_expected_node_error_stops_the_run(registry):
    nodes = [TRIGGER, N("bad", "test.fail"), N("later", "test.echo")]
    edges = [E("start", "payload", "bad"), E("bad", "result", "later")]
    run = execute(wf(nodes, edges), registry)
    assert run.status == RunStatus.FAILED
    assert statuses(run) == {"start": NodeStatus.COMPLETED, "bad": NodeStatus.FAILED, "later": NodeStatus.SKIPPED}
    assert run.nodes["bad"].error == {"code": "test.expected_failure", "message": "This failure is expected.",
                                      "details": {"why": "test"}}
    assert run.nodes["later"].reason == "Run stopped: node 'bad' failed."
    assert CALLS == ["bad"]
    final = run.events.all()[-1]
    assert final.type == EventType.RUN_FAILED and final.payload["failed_node"] == "bad"


def test_unexpected_exception_is_reported_not_swallowed(registry):
    run = execute(wf([TRIGGER, N("x", "test.boom")], [FROM_START]), registry)
    assert run.status == RunStatus.FAILED
    assert run.nodes["x"].error["code"] == "node.unexpected_error"
    assert "kaboom" in run.nodes["x"].error["message"]
    assert run.nodes["x"].outputs == {}                              # no fabricated result


def test_handler_error_inside_condition_is_structured(registry):
    nodes = [N("start", "core.manual_trigger", payload={"v": 1}),
             N("check", "core.condition", operator="greater_than", compare_to=3)]
    run = execute(wf(nodes, [E("start", "payload", "check")]), registry)
    assert run.status == RunStatus.FAILED
    assert run.nodes["check"].error["code"] == "condition.type_error"


@pytest.mark.parametrize("node_type,code", [
    ("test.undeclared", "node.invalid_result"),
    ("test.wrong_type", "node.invalid_result"),
    ("test.not_result", "node.invalid_result"),
])
def test_bad_handler_results_are_rejected(registry, node_type, code):
    run = execute(wf([TRIGGER, N("x", node_type)], [FROM_START]), registry)
    assert run.status == RunStatus.FAILED
    assert run.nodes["x"].error["code"] == code
    assert run.nodes["x"].outputs == {}


def test_runtime_input_type_check(registry):
    # condition outputs are ANY, so validation allows this edge; the value is an object, not text
    nodes = [TRIGGER, N("check", "core.condition", operator="is_truthy"), N("x", "test.strict_text")]
    edges = [E("start", "payload", "check"), E("check", "true", "x")]
    run = execute(wf(nodes, edges), registry)
    assert run.status == RunStatus.FAILED
    assert run.nodes["x"].error["code"] == "node.invalid_input"
    assert CALLS == []                                               # handler was never called


def test_matches_type_rules():
    assert matches_type(3, DataType.NUMBER) and matches_type(2.5, DataType.NUMBER)
    assert not matches_type(True, DataType.NUMBER)
    assert matches_type(True, DataType.BOOLEAN) and not matches_type(1, DataType.BOOLEAN)
    assert matches_type({}, DataType.OBJECT) and not matches_type([], DataType.OBJECT)
    assert matches_type([], DataType.LIST) and matches_type(None, DataType.ANY)


# ---- timeouts -----------------------------------------------------------------------------
def test_timeout_fails_the_node_and_interrupts_the_handler(registry):
    t0 = time.monotonic()
    run = execute(wf([TRIGGER, N("slow", "test.slow", seconds=5)], [E("start", "payload", "slow")]), registry)
    assert time.monotonic() - t0 < 2                                 # did not wait the full 5s
    assert run.status == RunStatus.FAILED
    assert run.nodes["slow"].error["code"] == "node.timeout"
    assert FLAGS.get("slow.interrupted") is True                     # handler was really cancelled


# ---- cancellation -------------------------------------------------------------------------
async def _cancel_when_started(run: Run, node_id: str):
    task = asyncio.ensure_future(run.execute())
    for _ in range(200):
        if run.nodes[node_id].status == NodeStatus.RUNNING:
            break
        await asyncio.sleep(0.01)
    run.cancel()
    await task


def test_cancel_during_a_running_node(registry):
    nodes = [TRIGGER, N("slow", "test.slow_long", seconds=10), N("after", "test.echo")]
    edges = [E("start", "payload", "slow"), E("slow", "result", "after")]
    run = Run(wf(nodes, edges), registry)
    t0 = time.monotonic()
    asyncio.run(_cancel_when_started(run, "slow"))
    assert time.monotonic() - t0 < 2
    assert run.status == RunStatus.CANCELLED
    assert statuses(run) == {"start": NodeStatus.COMPLETED, "slow": NodeStatus.CANCELLED, "after": NodeStatus.CANCELLED}
    assert FLAGS.get("slow.interrupted") is True
    assert "after" not in CALLS
    last = run.events.all()[-1]
    assert last.type == EventType.RUN_CANCELLED
    assert last.payload["running_node"] == "slow" and last.payload["not_started"] == ["after"]
    assert [e.type for e in run.events.all() if e.type in TERMINAL_EVENTS] == [EventType.RUN_CANCELLED]


def test_cancel_before_execution_starts(registry):
    run = Run(wf([TRIGGER, N("x", "test.echo")], [FROM_START]), registry)
    run.cancel()
    asyncio.run(run.execute())
    assert run.status == RunStatus.CANCELLED
    assert CALLS == []
    assert set(statuses(run).values()) == {NodeStatus.CANCELLED}


def test_cancel_from_another_thread(registry):
    run = Run(wf([TRIGGER, N("slow", "test.slow_long", seconds=10)], [E("start", "payload", "slow")]), registry)

    async def main():
        task = asyncio.ensure_future(run.execute())
        while run.nodes["slow"].status != NodeStatus.RUNNING:
            await asyncio.sleep(0.01)
        threading.Thread(target=run.cancel).start()
        await task

    asyncio.run(main())
    assert run.status == RunStatus.CANCELLED


def test_non_cancellable_node_finishes_then_run_is_cancelled(registry):
    nodes = [TRIGGER, N("fixed", "test.slow_fixed", seconds=0.2), N("after", "test.echo")]
    edges = [E("start", "payload", "fixed"), E("fixed", "result", "after")]
    run = Run(wf(nodes, edges), registry)
    asyncio.run(_cancel_when_started(run, "fixed"))
    assert run.status == RunStatus.CANCELLED
    assert run.nodes["fixed"].status == NodeStatus.COMPLETED         # it was allowed to finish
    assert run.nodes["fixed"].outputs == {"result": "finished"}
    assert run.nodes["after"].status == NodeStatus.CANCELLED and "after" not in CALLS


def test_cancel_after_completion_is_a_noop(registry):
    run = execute(wf([TRIGGER]), registry)
    run.cancel()
    assert run.status == RunStatus.COMPLETED
    assert [e.type for e in run.events.all() if e.type in TERMINAL_EVENTS] == [EventType.RUN_COMPLETED]


def test_cancelling_the_asyncio_task_leaves_a_terminal_state(registry):
    run = Run(wf([TRIGGER, N("slow", "test.slow_long", seconds=10)], [E("start", "payload", "slow")]), registry)

    async def main():
        task = asyncio.ensure_future(run.execute())
        while run.nodes["slow"].status != NodeStatus.RUNNING:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert run.status == RunStatus.CANCELLED
    assert FLAGS.get("slow.interrupted") is True


# ---- progress and listeners ---------------------------------------------------------------
def test_progress_events_are_clamped_and_attributed(registry):
    definition = wf([TRIGGER, N("p", "test.progress")])
    # 'p' has no inputs and is not a trigger -> unreachable warning, but it still runs
    run = execute(definition, registry)
    progress = [e for e in run.events.all() if e.type == EventType.NODE_PROGRESS]
    assert [e.payload for e in progress] == [
        {"fraction": 0.25, "message": "quarter"}, {"fraction": 1.0, "message": "clamped"},
        {"fraction": None, "message": "indeterminate"}]
    assert {e.node_id for e in progress} == {"p"}


def test_listeners_receive_live_events_and_cannot_break_a_run(registry):
    run = Run(wf([TRIGGER, N("x", "test.echo")], [FROM_START]), registry)
    seen = []
    run.events.subscribe(seen.append)

    def broken(event):
        raise RuntimeError("listener bug")

    run.events.subscribe(broken)
    asyncio.run(run.execute())
    assert run.status == RunStatus.COMPLETED
    assert [e.type for e in seen] == types(run)[1:]                  # everything after RUN_CREATED


def test_snapshot_is_json_friendly(registry):
    import json
    run = execute(wf([TRIGGER, N("x", "test.echo")], [FROM_START]), registry)
    data = json.loads(json.dumps(run.snapshot()))
    assert data["status"] == "completed" and data["nodes"]["x"]["status"] == "completed"
