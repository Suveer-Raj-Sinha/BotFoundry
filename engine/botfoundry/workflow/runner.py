"""Sequential workflow execution with branching, events, timeouts and cancellation.

Failure policy: the first failed node stops the run; nodes that never ran are marked skipped
with the reason recorded. Nothing is ever reported as successful unless it actually ran."""
import asyncio
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Sequence

from ..rules.constraints import SystemConstraints, default_constraints
from ..rules.evaluate import evaluate_action
from ..rules.models import Rulebook
from ..rules.permissions import Policy, known_permission_names
from .approvals import Approval, ApprovalNotFound, ApprovalNotPending, ApprovalStatus
from .definition import NodeInstance, WorkflowDefinition
from .events import EventLog, EventType
from .node import CancelToken, NodeContext, NodeError, NodeResult, NodeSpec, RunBudget, RunCancelled
from .registry import NodeRegistry
from .types import matches_type
from .validation import Severity, ValidationIssue, validate_workflow

log = logging.getLogger(__name__)


class RunStatus(str, Enum):
    CREATED = "created"
    VALIDATING = "validating"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class _Timeout(Exception):
    pass


class _RunTimeLimit(Exception):
    pass


_TICK = 0.05  # how often a running node is checked against its timeout, the run limit and cancellation


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

    def __init__(self, definition: WorkflowDefinition, registry: NodeRegistry, run_id: str | None = None, *,
                 rulebook: Rulebook | None = None, rulebook_version: int | None = None,
                 constraints: SystemConstraints | None = None, approval_timeout: float | None = None) -> None:
        self.definition = definition
        self.registry = registry
        # The Rulebook is pinned for the whole run: saving a newer version mid-run changes nothing here.
        self.rulebook = rulebook
        self.rulebook_version = rulebook_version
        self.rulebook_bot_id = rulebook.bot_id if rulebook else None
        self.constraints = constraints or default_constraints()
        self.budget = RunBudget(max_files=rulebook.limits.max_files_per_run if rulebook else None)
        self._run_limit = rulebook.limits.max_run_seconds if rulebook else None
        self._approval_timeout = approval_timeout or (rulebook.approval.timeout_seconds if rulebook else 300)
        self._known_permissions = known_permission_names(p for spec in registry.all() for p in spec.permissions)
        self._approvals: dict[str, Approval] = {}
        self._approval_lock = threading.Lock()
        self._approval_counter = 0
        self._pause_depth = 0  # >0 while waiting for a person: run and node clocks stand still
        self._active = 0.0
        self._last: float | None = None
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
            "workflow_id": definition.id, "workflow_name": definition.name, "node_count": len(definition.nodes),
            "rulebook": {"bot_id": self.rulebook_bot_id, "version": rulebook_version} if rulebook else None})

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
            "rulebook": {"bot_id": self.rulebook_bot_id, "version": self.rulebook_version} if self.rulebook else None,
            "pending_approvals": [a.to_dict() for a in self._approvals.values() if a.status == ApprovalStatus.PENDING],
            "nodes": {k: v.to_dict() for k, v in self.nodes.items()},
        }

    def pending_approvals(self) -> list[Approval]:
        return [a for a in self._approvals.values() if a.status == ApprovalStatus.PENDING]

    def respond(self, approval_id: str, approve: bool, note: str | None = None) -> Approval:
        """Answer an approval request. Safe from any thread. `note` is free text kept with the answer.
        Raises ApprovalNotFound / ApprovalNotPending."""
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise ApprovalNotFound(f"This run has no approval '{approval_id}'.")
        if not self._decide(approval, approve, "approved" if approve else "denied", decided_by="user", note=note):
            raise ApprovalNotPending(f"Approval '{approval_id}' was already answered ({approval.status.value}).")
        loop, future = self._loop, approval.future
        if loop is not None and future is not None:  # wake the run that is waiting
            def wake() -> None:
                if not future.done():
                    future.set_result(approval.status == ApprovalStatus.GRANTED)
            try:
                loop.call_soon_threadsafe(wake)
            except RuntimeError:  # loop already closed; the decision itself is recorded
                pass
        return approval

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
        if result.required_permissions and self.rulebook is None:
            needed = ", ".join(result.required_permissions)
            reason = ("This workflow has no bot_id" if self.definition.bot_id is None
                      else f"Bot '{self.definition.bot_id}' has no Rulebook")
            result.issues.append(ValidationIssue(
                code="rulebook.required", severity=Severity.ERROR,
                message=f"{reason}, but its nodes need permissions ({needed}). Set bot_id and save a Rulebook "
                        "for that bot before running."))
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
        self._last = time.monotonic()  # the run clock starts here
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
            try:
                permitted = await self._enforce_permissions(node, spec)  # deny / ask happen BEFORE any code runs
                ctx = NodeContext(run_id=self.run_id, node_id=node.id, cancel=self.cancel_token,
                                  report_progress=self._progress_callback(node.id),
                                  authorize=self._authorizer(node.id, spec, permitted),
                                  request_approval=self._approval_requester(node.id), budget=self.budget)
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
            except _RunTimeLimit:
                limit = self._run_limit
                self._fail_node(record, {"code": "run.time_limit", "details": {"max_run_seconds": limit},
                                         "message": f"The run exceeded the {limit:g}s limit in the bot's Rulebook."},
                                time.monotonic() - t0)
                self._stop_after_failure(order, position, node.id)
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
                # The whole run task was cancelled from outside (Ctrl+C in the CLI, forced shutdown).
                # Record the node that was running before letting the cancellation continue.
                record.status = NodeStatus.CANCELLED
                record.finished_at = _now()
                record.reason = "Cancelled while running."
                self._finish_cancelled("The run's task was cancelled.", [n.id for n in order[position + 1:]],
                                       running_node=node.id)
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
        node_active, last = 0.0, time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait(waiting, timeout=_TICK, return_when=asyncio.FIRST_COMPLETED)
                now = time.monotonic()
                if self._pause_depth == 0:  # time spent waiting for a person never counts against the node
                    node_active += now - last
                last = now
                if task in done:
                    return task.result()
                if cancel_waiter in done:
                    if spec.cancellable:
                        await self._abort(task)
                        raise _CancelledWhileRunning()
                    waiting = {task}  # not cancellable: let it finish, honor the cancel afterwards
                    continue
                if node_active >= spec.timeout_seconds:
                    await self._abort(task)
                    raise _Timeout()
                if self._run_limit is not None and self._active_seconds() >= self._run_limit:
                    await self._abort(task)
                    raise _RunTimeLimit()
        except asyncio.CancelledError:
            await self._abort(task)  # the whole run task was cancelled: do not leave the handler running
            raise
        finally:
            cancel_waiter.cancel()

    # ---- permissions and approvals ----------------------------------------------------------
    def _active_seconds(self) -> float:
        """Run time that counts against max_run_seconds (excludes time spent waiting for a person)."""
        now = time.monotonic()
        if self._last is not None and self._pause_depth == 0:
            self._active += now - self._last
        self._last = now
        return self._active

    async def _enforce_permissions(self, node: NodeInstance, spec: NodeSpec) -> set[str]:
        """Check every permission the node declares against the pinned Rulebook and the system limits.
        Denied: the node fails and nothing runs. Needs approval: the run pauses until a person answers.
        Returns the permissions the node may use."""
        permitted: set[str] = set()
        asks: list[str] = []
        for permission in sorted(spec.permissions):
            decision = evaluate_action(self.rulebook, self.constraints, permission, (),
                                       known_permissions=self._known_permissions)
            if decision.outcome == Policy.DENY:
                raise NodeError("permission.denied", decision.message,
                                {"permission": permission, "reason": decision.code})
            if decision.outcome == Policy.ASK:
                asks.append(permission)
            permitted.add(permission)
        if asks:
            approval = await self._request_approval(
                node.id, "node_permissions",
                f"Node '{node.id}' ({spec.title}) needs your approval to use: {', '.join(asks)}.",
                {"node_type": node.type, "permissions": asks})
            self._require_granted(approval)
        return permitted

    def _require_granted(self, approval: Approval) -> None:
        if approval.status == ApprovalStatus.GRANTED:
            return
        if approval.reason == "cancelled":
            raise _CancelledWhileRunning()
        why = {"timeout": "nobody answered in time", "denied": "it was denied"}.get(approval.reason or "", approval.reason)
        note = f": {approval.note}" if approval.note else ""
        raise NodeError("node.approval_denied", f"Approval was not given ({why}{note}).",
                        {"approval_id": approval.approval_id, "reason": approval.reason, "note": approval.note})

    def _authorizer(self, node_id: str, spec: NodeSpec, permitted: set[str]):
        def authorize(permission: str, paths: Sequence[str] = ()) -> list[str]:
            if permission not in spec.permissions:
                raise NodeError("permission.undeclared",
                                f"Node '{node_id}' did not declare the permission '{permission}'.",
                                {"permission": permission})
            if permission not in permitted:
                raise NodeError("permission.denied", f"'{permission}' is not permitted for this node.",
                                {"permission": permission})
            decision = evaluate_action(self.rulebook, self.constraints, permission, paths,
                                       known_permissions=self._known_permissions)
            if decision.outcome == Policy.DENY:
                raise NodeError(decision.code, decision.message, {
                    "permission": permission,
                    "refused_paths": [p.model_dump(mode="json") for p in decision.paths if not p.allowed]})
            # allow, or ask that was already approved for this node before it started
            return [p.resolved or p.path for p in decision.paths]
        return authorize

    def _approval_requester(self, node_id: str):
        async def request(summary: str, details: dict[str, Any] | None = None,
                          timeout_seconds: float | None = None) -> None:
            approval = await self._request_approval(node_id, "explicit", summary, details or {}, timeout_seconds)
            if approval.status != ApprovalStatus.GRANTED:
                if approval.reason == "cancelled":
                    raise RunCancelled()
                self._require_granted(approval)
        return request

    async def _request_approval(self, node_id: str, kind: str, summary: str, details: dict[str, Any],
                                timeout_seconds: float | None = None) -> Approval:
        """Pause until a person answers. No answer within the timeout is a DENIAL."""
        timeout = timeout_seconds or self._approval_timeout
        now = _now()
        with self._approval_lock:
            self._approval_counter += 1
            approval = Approval(approval_id=f"ap-{self._approval_counter}", run_id=self.run_id, node_id=node_id,
                                kind=kind, summary=summary, details=details, requested_at=now,
                                expires_at=now + timedelta(seconds=timeout),
                                future=asyncio.get_running_loop().create_future())
            self._approvals[approval.approval_id] = approval
        record = self.nodes[node_id]
        self._active_seconds()
        self._pause_depth += 1
        record.status = NodeStatus.WAITING_APPROVAL
        if self.status == RunStatus.RUNNING:
            self.status = RunStatus.WAITING_APPROVAL
        self.events.append(EventType.APPROVAL_REQUESTED, node_id, {
            "approval_id": approval.approval_id, "kind": kind, "summary": summary, "details": details,
            "timeout_seconds": timeout, "expires_at": approval.expires_at.isoformat()})
        cancel_waiter = asyncio.ensure_future(self._cancel_event.wait())
        try:
            done, _ = await asyncio.wait({approval.future, cancel_waiter}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if approval.future not in done:  # nobody answered
                self._decide(approval, False, "cancelled" if cancel_waiter in done else "timeout", decided_by="engine")
        except asyncio.CancelledError:
            self._decide(approval, False, "cancelled", decided_by="engine")
            raise
        finally:
            cancel_waiter.cancel()
            self._active_seconds()
            self._pause_depth = max(0, self._pause_depth - 1)
            if not self.pending_approvals() and self.status == RunStatus.WAITING_APPROVAL:
                self.status = RunStatus.RUNNING
            if record.status == NodeStatus.WAITING_APPROVAL:
                record.status = NodeStatus.RUNNING
        return approval

    def _decide(self, approval: Approval, approve: bool, reason: str, *, decided_by: str, note: str | None = None) -> bool:
        """Record the outcome exactly once. Returns False if it was already decided."""
        with self._approval_lock:
            if approval.status != ApprovalStatus.PENDING:
                return False
            approval.status = ApprovalStatus.GRANTED if approve else ApprovalStatus.DENIED
            approval.reason = reason
            approval.note = note
            approval.decided_at = _now()
        if approve:
            self.events.append(EventType.APPROVAL_GRANTED, approval.node_id,
                               {"approval_id": approval.approval_id, "decided_by": decided_by, "reason": reason,
                                "note": note})
        else:
            self.events.append(EventType.APPROVAL_DENIED, approval.node_id,
                               {"approval_id": approval.approval_id, "decided_by": decided_by, "reason": reason,
                                "note": note})
        return True

    def _close_open_approvals(self, reason: str) -> None:
        for approval in self.pending_approvals():  # a run never ends with an unanswered question
            self._decide(approval, False, reason, decided_by="engine")

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

    def _settle_running_nodes(self, status: NodeStatus, reason: str, error: dict[str, Any] | None = None) -> None:
        """Invariant: when a run ends, no node may still be marked running."""
        for record in self.nodes.values():
            if record.status in (NodeStatus.RUNNING, NodeStatus.WAITING_APPROVAL):
                record.status = status
                record.reason = reason
                record.error = error
                record.finished_at = _now()

    def _finish_failed(self, error: dict[str, Any], failed_node: str | None) -> None:
        self._close_open_approvals("run_failed")
        self._settle_running_nodes(NodeStatus.FAILED, "The run ended while this node was running.",
                                   {"code": "node.aborted", "details": {},
                                    "message": "The run ended while this node was running."})
        self.status = RunStatus.FAILED
        self.error = error
        self.finished_at = _now()
        self.events.append(EventType.RUN_FAILED, payload={
            "error": error, "failed_node": failed_node, "duration_ms": self._duration_ms()})

    def _finish_cancelled(self, reason: str, unstarted: list[str] | None = None, running_node: str | None = None) -> None:
        self._close_open_approvals("cancelled")
        self._settle_running_nodes(NodeStatus.CANCELLED, "Cancelled while running.")
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
