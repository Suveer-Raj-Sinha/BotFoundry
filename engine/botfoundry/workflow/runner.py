"""Sequential workflow execution with branching, events, timeouts and cancellation.

Failure policy: the first failed node stops the run; nodes that never ran are marked skipped
with the reason recorded. Nothing is ever reported as successful unless it actually ran."""
import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .definition import NodeInstance, WorkflowDefinition
from .events import EventLog, EventType
from .node import CancelToken, NodeContext, NodeError, NodeResult, NodeSpec, RunCancelled
from .registry import NodeRegistry
from .types import matches_type
from .validation import validate_workflow

log = logging.getLogger(__name__)


class RunStatus(str, Enum):
    CREATED = "created"
    VALIDATING = "validating"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class _Timeout(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class NodeRecord:
    node_id: str
    type: str
    status: NodeStatus = NodeStatus.PENDING
    outputs: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    error: dict[str, Any] | None = None
    reason: str | None = None  # why it was skipped / cancelled
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id, "type": self.type, "status": self.status.value,
            "outputs": self.outputs, "summary": self.summary, "error": self.error, "reason": self.reason,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


def _topological_order(definition: WorkflowDefinition) -> list[NodeInstance]:
    """Kahn's algorithm; ties broken by declaration order so runs are deterministic."""
    index = {n.id: i for i, n in enumerate(definition.nodes)}
    indegree = {n.id: 0 for n in definition.nodes}
    successors: dict[str, list[str]] = defaultdict(list)
    for edge in definition.edges:
        successors[edge.source].append(edge.target)
        indegree[edge.target] += 1
    ready = deque(sorted((i for i, d in indegree.items() if d == 0), key=index.get))  # type: ignore[arg-type]
    order: list[str] = []
    while ready:
        current = ready.popleft()
        order.append(current)
        released = []
        for nxt in successors[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                released.append(nxt)
        for nxt in sorted(released, key=index.get):  # type: ignore[arg-type]
            ready.append(nxt)
    by_id = {n.id: n for n in definition.nodes}
    return [by_id[i] for i in order]


class Run:
    """One execution of one workflow. Create it, optionally subscribe to `events`, then `await execute()`.
    `cancel()` is safe to call at any time and from any thread."""

    def __init__(self, definition: WorkflowDefinition, registry: NodeRegistry, run_id: str | None = None) -> None:
        self.definition = definition
        self.registry = registry
        self.run_id = run_id or uuid.uuid4().hex
        self.status = RunStatus.CREATED
        self.error: dict[str, Any] | None = None
        self.nodes: dict[str, NodeRecord] = {n.id: NodeRecord(node_id=n.id, type=n.type) for n in definition.nodes}
        self.events = EventLog(self.run_id)
        self.cancel_token = CancelToken()
        self.created_at = _now()
        self.finished_at: datetime | None = None
        self._cancel_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._t0 = time.monotonic()
        self.events.append(EventType.RUN_CREATED, payload={
            "workflow_id": definition.id, "workflow_name": definition.name, "node_count": len(definition.nodes)})

    # ---- public ---------------------------------------------------------------------------
    def cancel(self) -> None:
        self.cancel_token.cancel()
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._cancel_event.set)
            except RuntimeError:  # loop closed meanwhile; the token is already set
                pass

    @property
    def is_finished(self) -> bool:
        return self.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "workflow_id": self.definition.id, "status": self.status.value,
            "error": self.error, "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "nodes": {k: v.to_dict() for k, v in self.nodes.items()},
        }

    async def execute(self) -> "Run":
        if self._started:
            raise RuntimeError("A Run can only be executed once.")
        self._started = True
        self._loop = asyncio.get_running_loop()
        if self.cancel_token.cancelled:
            self._cancel_event.set()
        try:
            await self._execute()
        except asyncio.CancelledError:
            if not self.is_finished:
                self._finish_cancelled("The run's task was cancelled.")
            raise
        except Exception as exc:  # a bug in the engine itself: fail loudly, never hang
            log.exception("Internal error in run %s", self.run_id)
            if not self.is_finished:
                self._finish_failed({"code": "run.internal_error",
                                     "message": f"{type(exc).__name__}: {exc}", "details": {}}, None)
        return self

    # ---- execution ------------------------------------------------------------------------
    async def _execute(self) -> None:
        # 1. validation: an invalid workflow never starts
        self.status = RunStatus.VALIDATING
        self.events.append(EventType.VALIDATION_STARTED)
        result = validate_workflow(self.definition, self.registry)
        if not result.ok:
            self.events.append(EventType.VALIDATION_FAILED,
                               payload={"issues": [i.model_dump(mode="json") for i in result.issues]})
            self._finish_failed({"code": "validation_failed",
                                 "message": f"The workflow has {len(result.errors)} validation error(s).",
                                 "details": {}}, None)
            return
        self.events.append(EventType.VALIDATION_SUCCEEDED, payload={
            "warnings": [w.model_dump(mode="json") for w in result.warnings],
            "required_permissions": result.required_permissions})

        # 2. prepare
        specs: dict[str, NodeSpec] = {}
        configs: dict[str, Any] = {}
        for node in self.definition.nodes:
            spec = self.registry.get(node.type, node.version)
            assert spec is not None  # guaranteed by validation
            specs[node.id] = spec
            configs[node.id] = spec.config_model.model_validate(node.config)
        incoming = {(e.target, e.target_port): (e.source, e.source_port) for e in self.definition.edges}
        order = _topological_order(self.definition)
        outputs: dict[str, dict[str, Any]] = {}

        self.status = RunStatus.RUNNING
        for position, node in enumerate(order):
            if self.cancel_token.cancelled:
                self._finish_cancelled("Cancelled between nodes.", [n.id for n in order[position:]])
                return
            spec, record = specs[node.id], self.nodes[node.id]

            # gather inputs; an inactive required input means this node is skipped (branching)
            inputs: dict[str, Any] = {}
            skip_reason: str | None = None
            for port in spec.inputs:
                source = incoming.get((node.id, port.name))
                active = source is not None and source[1] in outputs.get(source[0], {})
                if active:
                    inputs[port.name] = outputs[source[0]][source[1]]  # type: ignore[index]
                elif port.required:
                    where = f"'{source[0]}.{source[1]}'" if source else "its source"
                    skip_reason = f"Required input '{port.name}' is inactive ({where} produced no value)."
                    break
            if skip_reason:
                self._skip(record, skip_reason)
                continue

            # runtime type check of inputs (ANY ports can carry anything into a typed port)
            bad = next((p for p in spec.inputs if p.name in inputs and not matches_type(inputs[p.name], p.type)), None)
            if bad is not None:
                self._start(record, node)
                self._fail_node(record, {"code": "node.invalid_input", "details": {"port": bad.name},
                                         "message": f"Input '{bad.name}' expected {bad.type.value}, "
                                                    f"got {type(inputs[bad.name]).__name__}."}, 0.0)
                self._stop_after_failure(order, position, node.id)
                return

            # run the node
            self._start(record, node)
            t0 = time.monotonic()
            ctx = NodeContext(run_id=self.run_id, node_id=node.id, cancel=self.cancel_token,
                              report_progress=self._progress_callback(node.id))
            try:
                result = await self._call_handler(spec, ctx, configs[node.id], inputs)
                problem = self._check_result(spec, result)
                if problem is not None:
                    raise NodeError("node.invalid_result", problem)
            except (RunCancelled, _CancelledWhileRunning):
                record.status = NodeStatus.CANCELLED
                record.finished_at = _now()
                record.reason = "Cancelled while running."
                self._finish_cancelled("Cancelled while a node was running.", [n.id for n in order[position + 1:]],
                                       running_node=node.id)
                return
            except _Timeout:
                self._fail_node(record, {"code": "node.timeout", "details": {"timeout_seconds": spec.timeout_seconds},
                                         "message": f"Node '{node.id}' exceeded its {spec.timeout_seconds:g}s timeout."},
                                time.monotonic() - t0)
                self._stop_after_failure(order, position, node.id)
                return
            except NodeError as err:
                self._fail_node(record, err.to_dict(), time.monotonic() - t0)
                self._stop_after_failure(order, position, node.id)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Node %s raised an unexpected exception", node.id)
                self._fail_node(record, {"code": "node.unexpected_error", "details": {"exception": type(exc).__name__},
                                         "message": f"{type(exc).__name__}: {exc}"}, time.monotonic() - t0)
                self._stop_after_failure(order, position, node.id)
                return

            record.status = NodeStatus.COMPLETED
            record.outputs = dict(result.outputs)
            record.summary = result.summary
            record.finished_at = _now()
            outputs[node.id] = record.outputs
            self.events.append(EventType.NODE_COMPLETED, node.id, {
                "summary": result.summary, "output_ports": sorted(record.outputs),
                "duration_ms": round((time.monotonic() - t0) * 1000)})

        # a cancel that arrived while the last node ran still counts
        if self.cancel_token.cancelled and any(r.status == NodeStatus.PENDING for r in self.nodes.values()):
            self._finish_cancelled("Cancelled.", [n for n, r in self.nodes.items() if r.status == NodeStatus.PENDING])
            return
        self._finish_completed()

    async def _call_handler(self, spec: NodeSpec, ctx: NodeContext, config: Any, inputs: dict[str, Any]) -> NodeResult:
        task = asyncio.ensure_future(spec.handler(ctx, config, inputs))
        cancel_waiter = asyncio.ensure_future(self._cancel_event.wait())
        waiting = {task, cancel_waiter}
        deadline = time.monotonic() + spec.timeout_seconds
        try:
            while True:
                done, _ = await asyncio.wait(waiting, timeout=max(deadline - time.monotonic(), 0),
                                             return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    return task.result()
                if cancel_waiter in done:
                    if spec.cancellable:
                        await self._abort(task)
                        raise _CancelledWhileRunning()
                    waiting = {task}  # not cancellable: let it finish, honor the cancel afterwards
                    continue
                await self._abort(task)  # nothing finished before the deadline
                raise _Timeout()
        except asyncio.CancelledError:
            await self._abort(task)  # the whole run task was cancelled: do not leave the handler running
            raise
        finally:
            cancel_waiter.cancel()

    @staticmethod
    async def _abort(task: "asyncio.Future[Any]") -> None:
        if not task.done():
            task.cancel()
        try:
            await task
        except BaseException:  # we are discarding it; its outcome no longer matters
            pass

    @staticmethod
    def _check_result(spec: NodeSpec, result: Any) -> str | None:
        if not isinstance(result, NodeResult):
            return f"Handler returned {type(result).__name__} instead of NodeResult."
        for name, value in result.outputs.items():
            port = spec.output(name)
            if port is None:
                return f"Handler produced undeclared output port '{name}'."
            if not matches_type(value, port.type):
                return f"Output '{name}' should be {port.type.value} but is {type(value).__name__}."
        return None

    # ---- state transitions ------------------------------------------------------------------
    def _progress_callback(self, node_id: str):
        def report(fraction: float | None, message: str) -> None:
            if fraction is not None:
                fraction = min(max(float(fraction), 0.0), 1.0)
            self.events.append(EventType.NODE_PROGRESS, node_id, {"fraction": fraction, "message": message})
        return report

    def _start(self, record: NodeRecord, node: NodeInstance) -> None:
        record.status = NodeStatus.RUNNING
        record.started_at = _now()
        self.events.append(EventType.NODE_STARTED, node.id, {"type": node.type, "version": node.version})

    def _skip(self, record: NodeRecord, reason: str) -> None:
        record.status = NodeStatus.SKIPPED
        record.reason = reason
        record.finished_at = _now()
        self.events.append(EventType.NODE_SKIPPED, record.node_id, {"reason": reason})

    def _fail_node(self, record: NodeRecord, error: dict[str, Any], seconds: float) -> None:
        record.status = NodeStatus.FAILED
        record.error = error
        record.finished_at = _now()
        self.events.append(EventType.NODE_FAILED, record.node_id,
                           {"error": error, "duration_ms": round(seconds * 1000)})

    def _stop_after_failure(self, order: list[NodeInstance], position: int, failed_id: str) -> None:
        for later in order[position + 1:]:
            self._skip(self.nodes[later.id], f"Run stopped: node '{failed_id}' failed.")
        self._finish_failed(self.nodes[failed_id].error or {}, failed_id)

    def _duration_ms(self) -> int:
        return round((time.monotonic() - self._t0) * 1000)

    def _finish_completed(self) -> None:
        self.status = RunStatus.COMPLETED
        self.finished_at = _now()
        counts = [r.status for r in self.nodes.values()]
        self.events.append(EventType.RUN_COMPLETED, payload={
            "completed": counts.count(NodeStatus.COMPLETED), "skipped": counts.count(NodeStatus.SKIPPED),
            "duration_ms": self._duration_ms()})

    def _finish_failed(self, error: dict[str, Any], failed_node: str | None) -> None:
        self.status = RunStatus.FAILED
        self.error = error
        self.finished_at = _now()
        self.events.append(EventType.RUN_FAILED, payload={
            "error": error, "failed_node": failed_node, "duration_ms": self._duration_ms()})

    def _finish_cancelled(self, reason: str, unstarted: list[str] | None = None, running_node: str | None = None) -> None:
        cancelled = []
        for node_id in unstarted if unstarted is not None else list(self.nodes):
            record = self.nodes[node_id]
            if record.status == NodeStatus.PENDING:
                record.status = NodeStatus.CANCELLED
                record.reason = "Run was cancelled before this node started."
                record.finished_at = _now()
                cancelled.append(node_id)
        self.status = RunStatus.CANCELLED
        self.finished_at = _now()
        self.events.append(EventType.RUN_CANCELLED, payload={
            "reason": reason, "running_node": running_node, "not_started": cancelled,
            "duration_ms": self._duration_ms()})


class _CancelledWhileRunning(Exception):
    pass


async def run_workflow(definition: WorkflowDefinition, registry: NodeRegistry) -> Run:
    """Convenience: create and execute a run, returning the finished Run."""
    return await Run(definition, registry).execute()
