"""The first small set of built-in nodes. They exist to prove the node contract; the file and
Fixer nodes arrive in later phases."""
import asyncio
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .node import Category, NodeContext, NodeError, NodeResult, NodeSpec
from .registry import NodeRegistry
from .types import DataType, PortSpec


# ---- core.manual_trigger ------------------------------------------------------------------
class ManualTriggerConfig(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


async def _manual_trigger(ctx: NodeContext, config: ManualTriggerConfig, inputs: dict[str, Any]) -> NodeResult:
    return NodeResult(outputs={"payload": config.payload}, summary="Started manually")


MANUAL_TRIGGER = NodeSpec(
    id="core.manual_trigger", version=1, title="Manual Trigger",
    description="Starts a workflow when the user presses Run. Emits a fixed payload.",
    category=Category.TRIGGER, config_model=ManualTriggerConfig,
    inputs=(), outputs=(PortSpec(name="payload", type=DataType.OBJECT),),
    handler=_manual_trigger, timeout_seconds=5,
)


# ---- core.condition -----------------------------------------------------------------------
class ConditionConfig(BaseModel):
    operator: Literal["equals", "not_equals", "greater_than", "less_than", "contains", "is_truthy"]
    compare_to: Any = None

    @model_validator(mode="after")
    def _needs_compare_to(self) -> "ConditionConfig":
        if self.operator != "is_truthy" and "compare_to" not in self.model_fields_set:
            raise ValueError(f"operator '{self.operator}' requires 'compare_to'")
        return self


def _evaluate(operator: str, value: Any, compare_to: Any) -> bool:
    try:
        if operator == "equals":
            return value == compare_to
        if operator == "not_equals":
            return value != compare_to
        if operator == "greater_than":
            return value > compare_to
        if operator == "less_than":
            return value < compare_to
        if operator == "contains":
            return compare_to in value
        return bool(value)
    except TypeError as exc:
        raise NodeError("condition.type_error",
                        f"Cannot apply '{operator}' to {type(value).__name__} and {type(compare_to).__name__}.",
                        {"operator": operator}) from exc


async def _condition(ctx: NodeContext, config: ConditionConfig, inputs: dict[str, Any]) -> NodeResult:
    value = inputs["value"]
    if _evaluate(config.operator, value, config.compare_to):
        return NodeResult(outputs={"true": value}, summary="Condition true")
    return NodeResult(outputs={"false": value}, summary="Condition false")


CONDITION = NodeSpec(
    id="core.condition", version=1, title="Condition",
    description="Routes the value to the 'true' or 'false' output. The other branch is skipped.",
    category=Category.LOGIC, config_model=ConditionConfig,
    inputs=(PortSpec(name="value"),),
    outputs=(PortSpec(name="true"), PortSpec(name="false")),
    handler=_condition, timeout_seconds=5,
)


# ---- core.transform -----------------------------------------------------------------------
class TransformConfig(BaseModel):
    operation: Literal["to_upper", "to_lower", "length", "to_string"]


async def _transform(ctx: NodeContext, config: TransformConfig, inputs: dict[str, Any]) -> NodeResult:
    value = inputs["value"]
    op = config.operation
    if op in ("to_upper", "to_lower"):
        if not isinstance(value, str):
            raise NodeError("transform.type_error", f"'{op}' needs text, got {type(value).__name__}.")
        result: Any = value.upper() if op == "to_upper" else value.lower()
    elif op == "length":
        if not isinstance(value, (str, list, dict)):
            raise NodeError("transform.type_error", f"'length' needs text, list or object, got {type(value).__name__}.")
        result = len(value)
    else:  # to_string
        result = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return NodeResult(outputs={"result": result}, summary=f"Applied {op}")


TRANSFORM = NodeSpec(
    id="core.transform", version=1, title="Transform",
    description="Applies a simple operation to a value: upper/lower case, length, or text conversion.",
    category=Category.DATA, config_model=TransformConfig,
    inputs=(PortSpec(name="value"),), outputs=(PortSpec(name="result"),),
    handler=_transform, timeout_seconds=5,
)

# ---- core.wait ----------------------------------------------------------------------------
class WaitConfig(BaseModel):
    seconds: float = Field(default=1.0, ge=0, le=60)


async def _wait(ctx: NodeContext, config: WaitConfig, inputs: dict[str, Any]) -> NodeResult:
    loop = asyncio.get_running_loop()
    end = loop.time() + config.seconds
    while True:
        ctx.cancel.raise_if_cancelled()
        left = end - loop.time()
        if left <= 0:
            break
        ctx.report_progress(1 - left / config.seconds, f"{left:.1f}s left")
        await asyncio.sleep(min(0.25, left))
    return NodeResult(outputs={"result": inputs["value"]}, summary=f"Waited {config.seconds:g}s")


WAIT = NodeSpec(
    id="core.wait", version=1, title="Wait",
    description="Pauses for a number of seconds (0 to 60), reporting progress, then passes the value through.",
    category=Category.LOGIC, config_model=WaitConfig,
    inputs=(PortSpec(name="value"),), outputs=(PortSpec(name="result"),),
    handler=_wait, timeout_seconds=90,
)

# ---- core.approval_gate -------------------------------------------------------------------
class ApprovalGateConfig(BaseModel):
    summary: str = Field(min_length=1, max_length=500)
    details: dict[str, Any] = Field(default_factory=dict)


async def _approval_gate(ctx: NodeContext, config: ApprovalGateConfig, inputs: dict[str, Any]) -> NodeResult:
    preview = json.dumps(inputs["value"], default=str, ensure_ascii=False)
    details = {**config.details, "value_preview": preview[:500] + ("..." if len(preview) > 500 else "")}
    await ctx.request_approval(config.summary, details)  # raises (failing the node) if denied or unanswered
    return NodeResult(outputs={"result": inputs["value"]}, summary="Approved")


APPROVAL_GATE = NodeSpec(
    id="core.approval_gate", version=1, title="Approval Gate",
    description="Pauses the run and shows a summary until a person approves. Denied or unanswered = the run stops.",
    category=Category.APPROVAL, config_model=ApprovalGateConfig,
    inputs=(PortSpec(name="value"),), outputs=(PortSpec(name="result"),),
    handler=_approval_gate, timeout_seconds=30,   # active time only: waiting for a person does not count
)

BUILTIN_NODES = (MANUAL_TRIGGER, CONDITION, TRANSFORM, WAIT, APPROVAL_GATE)


def build_default_registry() -> NodeRegistry:
    from ..files.nodes import FILE_NODES  # imported here to avoid a circular import

    registry = NodeRegistry()
    for spec in BUILTIN_NODES + FILE_NODES:
        registry.register(spec)
    return registry
