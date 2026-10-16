import copy

import pytest
from pydantic import BaseModel

from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.definition import WorkflowDefinition
from botfoundry.workflow.node import Category, NodeResult, NodeSpec
from botfoundry.workflow.types import DataType, PortSpec
from botfoundry.workflow.validation import Severity, parse_and_validate, validate_workflow


def good_workflow():
    return {
        "id": "demo", "name": "Demo",
        "nodes": [
            {"id": "start", "type": "core.manual_trigger", "config": {"payload": {"name": "x"}}},
            {"id": "check", "type": "core.condition", "config": {"operator": "is_truthy"}},
            {"id": "upper", "type": "core.transform", "config": {"operation": "to_upper"}},
            {"id": "lower", "type": "core.transform", "config": {"operation": "to_lower"}},
        ],
        "edges": [
            {"source": "start", "source_port": "payload", "target": "check", "target_port": "value"},
            {"source": "check", "source_port": "true", "target": "upper", "target_port": "value"},
            {"source": "check", "source_port": "false", "target": "lower", "target_port": "value"},
        ],
    }


@pytest.fixture()
def registry():
    return build_default_registry()


def result_for(data, registry):
    definition, result = parse_and_validate(data, registry)
    return definition, result


def codes(result):
    return [i.code for i in result.issues]


def test_valid_workflow_passes(registry):
    definition, result = result_for(good_workflow(), registry)
    assert definition is not None
    assert result.ok and result.issues == []
    assert result.required_permissions == []


def test_structurally_invalid_document(registry):
    definition, result = result_for({"id": "x", "name": "", "nodes": "nope", "extra": 1}, registry)
    assert definition is None and not result.ok
    assert set(codes(result)) == {"schema.invalid"}
    assert {i.path for i in result.issues} >= {"name", "nodes", "extra"}


def test_bad_node_id_pattern_is_schema_error(registry):
    data = good_workflow()
    data["nodes"][0]["id"] = "bad id!"
    definition, result = result_for(data, registry)
    assert definition is None and "schema.invalid" in codes(result)


def test_duplicate_node_id(registry):
    data = good_workflow()
    data["nodes"].append(copy.deepcopy(data["nodes"][2]))
    _, result = result_for(data, registry)
    assert "node.duplicate_id" in codes(result)


def test_unknown_node_type_and_version(registry):
    data = good_workflow()
    data["nodes"][2]["type"] = "core.nope"
    data["nodes"][3]["version"] = 9
    _, result = result_for(data, registry)
    messages = [i.message for i in result.issues if i.code == "node.unknown_type"]
    assert len(messages) == 2
    assert any("Unknown node type 'core.nope'" in m for m in messages)
    assert any("no version 9" in m and "[1]" in m for m in messages)


def test_invalid_config_reports_node_and_field(registry):
    data = good_workflow()
    data["nodes"][1]["config"] = {"operator": "equals"}          # missing compare_to
    data["nodes"][2]["config"] = {"operation": "explode"}        # not an allowed value
    _, result = result_for(data, registry)
    bad = [i for i in result.issues if i.code == "node.invalid_config"]
    assert {i.node_id for i in bad} == {"check", "upper"}
    assert any(i.path == "operation" for i in bad)


def test_edge_to_unknown_node(registry):
    data = good_workflow()
    data["edges"].append({"source": "ghost", "source_port": "x", "target": "upper", "target_port": "value"})
    _, result = result_for(data, registry)
    issue = next(i for i in result.issues if i.code == "edge.unknown_node")
    assert issue.edge_index == 3


def test_edge_to_unknown_ports(registry):
    data = good_workflow()
    data["edges"][0]["source_port"] = "nope"
    data["edges"][1]["target_port"] = "nope"
    _, result = result_for(data, registry)
    assert codes(result).count("edge.unknown_port") == 2


def test_type_mismatch(registry):
    registry_with_string = build_default_registry()

    async def h(ctx, cfg, inputs):
        return NodeResult()

    class Cfg(BaseModel):
        pass

    registry_with_string.register(NodeSpec(
        id="test.text_only", version=1, title="Text only", description="", category=Category.DATA,
        config_model=Cfg, inputs=(PortSpec(name="text", type=DataType.STRING),), outputs=(), handler=h))
    data = good_workflow()
    data["nodes"].append({"id": "t", "type": "test.text_only"})
    # payload is OBJECT, text input is STRING -> mismatch
    data["edges"].append({"source": "start", "source_port": "payload", "target": "t", "target_port": "text"})
    _, result = result_for(data, registry_with_string)
    assert "edge.type_mismatch" in codes(result)


def test_missing_required_input(registry):
    data = good_workflow()
    data["edges"].pop(0)  # nothing feeds 'check.value'
    _, result = result_for(data, registry)
    issue = next(i for i in result.issues if i.code == "node.missing_input")
    assert issue.node_id == "check"


def test_input_with_two_connections(registry):
    data = good_workflow()
    data["nodes"].append({"id": "t2", "type": "core.transform", "config": {"operation": "to_string"}})
    data["edges"] += [
        {"source": "start", "source_port": "payload", "target": "t2", "target_port": "value"},
        {"source": "check", "source_port": "true", "target": "t2", "target_port": "value"},
    ]
    _, result = result_for(data, registry)
    assert "edge.multiple_inputs" in codes(result)


def test_no_trigger_and_multiple_triggers(registry):
    data = good_workflow()
    no_trigger = copy.deepcopy(data)
    no_trigger["nodes"].pop(0)
    no_trigger["edges"].pop(0)
    assert "graph.no_trigger" in codes(result_for(no_trigger, registry)[1])

    two = copy.deepcopy(data)
    two["nodes"].append({"id": "start2", "type": "core.manual_trigger"})
    assert "graph.multiple_triggers" in codes(result_for(two, registry)[1])


def test_cycle_detected(registry):
    data = good_workflow()
    data["nodes"] += [
        {"id": "a", "type": "core.transform", "config": {"operation": "to_string"}},
        {"id": "b", "type": "core.transform", "config": {"operation": "to_string"}},
    ]
    data["edges"] += [
        {"source": "a", "source_port": "result", "target": "b", "target_port": "value"},
        {"source": "b", "source_port": "result", "target": "a", "target_port": "value"},
    ]
    _, result = result_for(data, registry)
    cycle = next(i for i in result.issues if i.code == "graph.cycle")
    assert "a" in cycle.message and "b" in cycle.message


def test_self_loop_is_a_cycle(registry):
    data = good_workflow()
    data["edges"].append({"source": "upper", "source_port": "result", "target": "upper", "target_port": "value"})
    _, result = result_for(data, registry)
    assert "graph.cycle" in codes(result)


def test_unreachable_node_is_a_warning_not_an_error(registry):
    data = good_workflow()
    data["nodes"].append({"id": "island", "type": "core.transform", "config": {"operation": "to_string"}})
    _, result = result_for(data, registry)
    warn = [i for i in result.issues if i.severity == Severity.WARNING]
    assert [w.code for w in warn] == ["node.unreachable"] and warn[0].node_id == "island"
    # it also has an unconnected required input, which IS an error
    assert "node.missing_input" in codes(result)


def test_required_permissions_are_collected(registry):
    async def h(ctx, cfg, inputs):
        return NodeResult()

    class Cfg(BaseModel):
        pass

    registry.register(NodeSpec(
        id="test.writer", version=1, title="W", description="", category=Category.FILES, config_model=Cfg,
        inputs=(PortSpec(name="value"),), outputs=(), handler=h,
        permissions=frozenset({"fs.write", "fs.read"})))
    data = good_workflow()
    data["nodes"].append({"id": "w", "type": "test.writer"})
    data["edges"].append({"source": "upper", "source_port": "result", "target": "w", "target_port": "value"})
    _, result = result_for(data, registry)
    assert result.ok
    assert result.required_permissions == ["fs.read", "fs.write"]


def test_multiple_problems_are_all_reported(registry):
    data = good_workflow()
    data["nodes"][2]["type"] = "core.nope"
    data["edges"][0]["source_port"] = "nope"
    _, result = result_for(data, registry)
    assert {"node.unknown_type", "edge.unknown_port"} <= set(codes(result))
    assert len(result.errors) >= 2


def test_validation_never_mutates_input(registry):
    data = good_workflow()
    before = copy.deepcopy(data)
    definition, _ = result_for(data, registry)
    assert data == before
    assert isinstance(definition, WorkflowDefinition)
    assert validate_workflow(definition, registry).ok
