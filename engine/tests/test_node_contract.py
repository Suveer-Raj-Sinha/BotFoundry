import asyncio

import pytest
from pydantic import BaseModel

from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.node import CancelToken, Category, NodeContext, NodeError, NodeResult, NodeSpec, RunCancelled
from botfoundry.workflow.registry import NodeRegistry, RegistryError
from botfoundry.workflow.types import DataType, PortSpec, is_compatible


class Empty(BaseModel):
    pass


async def _noop(ctx, config, inputs):
    return NodeResult()


def make_spec(**overrides):
    base = dict(id="test.node", version=1, title="T", description="d", category=Category.DATA,
                config_model=Empty, inputs=(), outputs=(), handler=_noop)
    base.update(overrides)
    return NodeSpec(**base)


def ctx():
    return NodeContext(run_id="r1", node_id="n1", cancel=CancelToken())


def run(spec_or_coro):
    return asyncio.run(spec_or_coro)


# ---- NodeSpec rules -----------------------------------------------------------------------
@pytest.mark.parametrize("bad_id", ["nodots", "Core.Upper", "core.", ".core", "1core.x", "core..x"])
def test_invalid_node_ids_rejected(bad_id):
    with pytest.raises(ValueError):
        make_spec(id=bad_id)


def test_duplicate_port_names_rejected():
    with pytest.raises(ValueError):
        make_spec(inputs=(PortSpec(name="a"), PortSpec(name="a")))


@pytest.mark.parametrize("kwargs", [{"version": 0}, {"timeout_seconds": 0}, {"timeout_seconds": -1}])
def test_invalid_numbers_rejected(kwargs):
    with pytest.raises(ValueError):
        make_spec(**kwargs)


def test_describe_is_json_friendly():
    import json
    info = build_default_registry().get("core.condition", 1).describe()
    json.dumps(info)  # must not raise
    assert info["category"] == "logic"
    assert "operator" in info["config_schema"]["properties"]


# ---- types --------------------------------------------------------------------------------
def test_type_compatibility():
    assert is_compatible(DataType.STRING, DataType.STRING)
    assert is_compatible(DataType.ANY, DataType.NUMBER)
    assert is_compatible(DataType.LIST, DataType.ANY)
    assert not is_compatible(DataType.STRING, DataType.NUMBER)


# ---- registry -----------------------------------------------------------------------------
def test_registry_lookup_and_duplicates():
    registry = NodeRegistry()
    registry.register(make_spec())
    registry.register(make_spec(version=2))
    assert registry.get("test.node", 1) is not None
    assert registry.get("test.node", 3) is None
    assert registry.versions("test.node") == [1, 2]
    with pytest.raises(RegistryError):
        registry.register(make_spec())


def test_default_registry_contents():
    ids = [s.id for s in build_default_registry().all()]
    assert ids == ["core.approval_gate", "core.condition", "core.manual_trigger", "core.transform", "core.wait",
                   "files.filter", "files.find_duplicates", "files.hash", "files.scan_folder"]


# ---- cancellation token -------------------------------------------------------------------
def test_cancel_token():
    token = CancelToken()
    token.raise_if_cancelled()  # no-op
    token.cancel()
    assert token.cancelled
    with pytest.raises(RunCancelled):
        token.raise_if_cancelled()


# ---- built-in node handlers ---------------------------------------------------------------
def call(node_id, config, inputs):
    spec = build_default_registry().get(node_id, 1)
    parsed = spec.config_model.model_validate(config)
    return run(spec.handler(ctx(), parsed, inputs))


def test_manual_trigger_emits_payload():
    result = call("core.manual_trigger", {"payload": {"a": 1}}, {})
    assert result.outputs == {"payload": {"a": 1}}


@pytest.mark.parametrize("operator,value,compare_to,branch", [
    ("equals", 5, 5, "true"), ("equals", 5, 6, "false"),
    ("not_equals", "a", "b", "true"),
    ("greater_than", 10, 3, "true"), ("greater_than", 1, 3, "false"),
    ("less_than", 1, 3, "true"),
    ("contains", "hello world", "world", "true"), ("contains", [1, 2], 3, "false"),
])
def test_condition_routes_to_one_branch(operator, value, compare_to, branch):
    result = call("core.condition", {"operator": operator, "compare_to": compare_to}, {"value": value})
    assert list(result.outputs) == [branch]          # exactly one active port
    assert result.outputs[branch] == value


def test_condition_is_truthy_needs_no_compare_to():
    assert list(call("core.condition", {"operator": "is_truthy"}, {"value": ""}).outputs) == ["false"]
    assert list(call("core.condition", {"operator": "is_truthy"}, {"value": "x"}).outputs) == ["true"]


def test_condition_requires_compare_to_for_other_operators():
    spec = build_default_registry().get("core.condition", 1)
    with pytest.raises(ValueError):
        spec.config_model.model_validate({"operator": "equals"})


def test_condition_type_error_is_structured():
    with pytest.raises(NodeError) as info:
        call("core.condition", {"operator": "greater_than", "compare_to": 3}, {"value": "text"})
    assert info.value.code == "condition.type_error"


@pytest.mark.parametrize("operation,value,expected", [
    ("to_upper", "abc", "ABC"), ("to_lower", "ABC", "abc"),
    ("length", "abcd", 4), ("length", [1, 2, 3], 3),
    ("to_string", 12, "12"), ("to_string", {"b": 1, "a": 2}, '{"a": 2, "b": 1}'),
])
def test_transform_operations(operation, value, expected):
    assert call("core.transform", {"operation": operation}, {"value": value}).outputs == {"result": expected}


def test_transform_type_errors():
    with pytest.raises(NodeError) as info:
        call("core.transform", {"operation": "to_upper"}, {"value": 5})
    assert info.value.code == "transform.type_error"
    with pytest.raises(NodeError):
        call("core.transform", {"operation": "length"}, {"value": 5})
