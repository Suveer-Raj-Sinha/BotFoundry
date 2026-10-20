import asyncio
import json
from pathlib import Path

import pytest

from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.runner import NodeStatus, RunStatus, run_workflow
from botfoundry.workflow.validation import parse_and_validate

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
FILES = sorted(EXAMPLES.glob("*.workflow.json"))


def load(name):
    registry = build_default_registry()
    definition, result = parse_and_validate(json.loads((EXAMPLES / name).read_text(encoding="utf-8")), registry)
    return definition, result, registry


def test_examples_exist():
    assert {f.name for f in FILES} >= {"hello.workflow.json", "branching.workflow.json",
                                       "failing.workflow.json", "slow.workflow.json"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_example_is_a_valid_workflow(path):
    _, result, _ = load(path.name)
    assert result.ok, [i.message for i in result.errors]


def test_hello_completes():
    definition, _, registry = load("hello.workflow.json")
    run = asyncio.run(run_workflow(definition, registry))
    assert run.status == RunStatus.COMPLETED
    assert run.nodes["shout"].outputs == {"result": '{"GREETING": "HELLO"}'}


def test_branching_runs_one_side_only():
    definition, _, registry = load("branching.workflow.json")
    run = asyncio.run(run_workflow(definition, registry))
    assert run.status == RunStatus.COMPLETED
    assert run.nodes["many"].status == NodeStatus.COMPLETED and run.nodes["few"].status == NodeStatus.SKIPPED
    assert run.nodes["many"].outputs == {"result": "3"}


def test_failing_example_fails_honestly():
    definition, _, registry = load("failing.workflow.json")
    run = asyncio.run(run_workflow(definition, registry))
    assert run.status == RunStatus.FAILED and run.error["code"] == "condition.type_error"
    assert run.nodes["never_runs"].status == NodeStatus.SKIPPED
