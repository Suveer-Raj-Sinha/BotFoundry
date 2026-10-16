"""Validate a workflow definition against the node registry. Never raises for bad workflows:
every problem becomes a structured issue the UI can show next to the right node or edge."""
from collections import defaultdict, deque
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError, computed_field

from .definition import WorkflowDefinition
from .node import Category, NodeSpec
from .registry import NodeRegistry
from .types import is_compatible


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(BaseModel):
    code: str  # stable machine-readable code, e.g. "edge.type_mismatch"
    severity: Severity
    message: str
    node_id: str | None = None
    edge_index: int | None = None
    path: str | None = None  # field path inside a node's config or the document


class ValidationResult(BaseModel):
    issues: list[ValidationIssue] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return not any(i.severity == Severity.ERROR for i in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]


def _path(loc: tuple[Any, ...]) -> str:
    return ".".join(str(p) for p in loc)


def validate_workflow(definition: WorkflowDefinition, registry: NodeRegistry) -> ValidationResult:
    issues: list[ValidationIssue] = []

    def error(code: str, message: str, **where: Any) -> None:
        issues.append(ValidationIssue(code=code, severity=Severity.ERROR, message=message, **where))

    def warning(code: str, message: str, **where: Any) -> None:
        issues.append(ValidationIssue(code=code, severity=Severity.WARNING, message=message, **where))

    # --- nodes -----------------------------------------------------------------------------
    specs: dict[str, NodeSpec] = {}
    node_ids: set[str] = set()
    for node in definition.nodes:
        if node.id in node_ids:
            error("node.duplicate_id", f"Node id '{node.id}' is used more than once.", node_id=node.id)
            continue
        node_ids.add(node.id)

        spec = registry.get(node.type, node.version)
        if spec is None:
            available = registry.versions(node.type)
            if available:
                msg = f"Node type '{node.type}' has no version {node.version} (available: {available})."
            else:
                msg = f"Unknown node type '{node.type}'."
            error("node.unknown_type", msg, node_id=node.id)
            continue
        specs[node.id] = spec

        try:
            spec.config_model.model_validate(node.config)
        except ValidationError as exc:
            for err in exc.errors():
                error("node.invalid_config", f"Invalid configuration: {err['msg']}",
                      node_id=node.id, path=_path(err["loc"]))

    # --- edges -----------------------------------------------------------------------------
    inbound: dict[tuple[str, str], list[int]] = defaultdict(list)
    graph_edges: list[tuple[str, str]] = []
    for index, edge in enumerate(definition.edges):
        usable = True
        for role, node_id in (("source", edge.source), ("target", edge.target)):
            if node_id not in node_ids:
                error("edge.unknown_node", f"Edge {role} node '{node_id}' does not exist.", edge_index=index)
                usable = False
        if not usable:
            continue

        source_spec, target_spec = specs.get(edge.source), specs.get(edge.target)
        source_port = source_spec.output(edge.source_port) if source_spec else None
        target_port = target_spec.input(edge.target_port) if target_spec else None
        if source_spec and source_port is None:
            error("edge.unknown_port",
                  f"Node '{edge.source}' has no output port '{edge.source_port}'.",
                  edge_index=index, node_id=edge.source)
            usable = False
        if target_spec and target_port is None:
            error("edge.unknown_port",
                  f"Node '{edge.target}' has no input port '{edge.target_port}'.",
                  edge_index=index, node_id=edge.target)
            usable = False
        if not usable:
            continue
        if source_port and target_port and not is_compatible(source_port.type, target_port.type):
            error("edge.type_mismatch",
                  f"Cannot connect '{edge.source}.{edge.source_port}' ({source_port.type.value}) "
                  f"to '{edge.target}.{edge.target_port}' ({target_port.type.value}).",
                  edge_index=index)
            continue

        inbound[(edge.target, edge.target_port)].append(index)
        graph_edges.append((edge.source, edge.target))

    for (target, port), indexes in inbound.items():
        if len(indexes) > 1:
            error("edge.multiple_inputs",
                  f"Input '{target}.{port}' has {len(indexes)} incoming connections; only one is allowed.",
                  node_id=target)

    for node_id, spec in specs.items():
        for port in spec.inputs:
            if port.required and (node_id, port.name) not in inbound:
                error("node.missing_input",
                      f"Required input '{port.name}' of node '{node_id}' is not connected.",
                      node_id=node_id)

    # --- graph shape -----------------------------------------------------------------------
    triggers = [n for n in node_ids if n in specs and specs[n].category == Category.TRIGGER]
    if not triggers:
        error("graph.no_trigger", "The workflow needs exactly one trigger node.")
    elif len(triggers) > 1:
        error("graph.multiple_triggers",
              f"The workflow has {len(triggers)} trigger nodes ({', '.join(sorted(triggers))}); exactly one is allowed.")

    successors: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = {n: 0 for n in node_ids}
    for source, target in graph_edges:
        successors[source].append(target)
        indegree[target] += 1

    queue = deque(n for n, d in indegree.items() if d == 0)
    remaining = dict(indegree)
    while queue:
        current = queue.popleft()
        remaining.pop(current, None)
        for nxt in successors[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    stuck = sorted(n for n in node_ids if indegree[n] > 0)
    if stuck:
        error("graph.cycle", f"The workflow contains a cycle involving or depending on: {', '.join(stuck)}.")

    if len(triggers) == 1:
        reached = {triggers[0]}
        frontier = deque(reached)
        while frontier:
            for nxt in successors[frontier.popleft()]:
                if nxt not in reached:
                    reached.add(nxt)
                    frontier.append(nxt)
        for node_id in sorted(node_ids - reached):
            warning("node.unreachable", f"Node '{node_id}' cannot be reached from the trigger and will never run.",
                    node_id=node_id)

    permissions = sorted({p for spec in specs.values() for p in spec.permissions})
    return ValidationResult(issues=issues, required_permissions=permissions)


def parse_and_validate(data: Any, registry: NodeRegistry) -> tuple[WorkflowDefinition | None, ValidationResult]:
    """Validate raw JSON data. Returns (definition, result); definition is None if the
    document is structurally invalid."""
    try:
        definition = WorkflowDefinition.model_validate(data)
    except ValidationError as exc:
        issues = [
            ValidationIssue(code="schema.invalid", severity=Severity.ERROR,
                            message=err["msg"], path=_path(err["loc"]))
            for err in exc.errors()
        ]
        return None, ValidationResult(issues=issues)
    return definition, validate_workflow(definition, registry)
