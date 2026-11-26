"""ctx.preflight: "would the Rulebook allow this?" answered without doing anything, without needing the node to declare
the permission, and without ever pausing for approval."""
import asyncio
import os

import pytest
from pydantic import BaseModel

from botfoundry.rules.constraints import SystemConstraints
from botfoundry.rules.models import Rulebook
from botfoundry.rules.paths import native_style
from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.definition import WorkflowDefinition
from botfoundry.workflow.events import EventType
from botfoundry.workflow.node import Category, NodeContext, NodeResult, NodeSpec
from botfoundry.workflow.runner import Run, RunStatus
from botfoundry.workflow.types import DataType, PortSpec

# Temporary folders live under protected system locations on Windows (AppData), so the tests use their own rules.
CONSTRAINTS = SystemConstraints(protected_paths=(), too_broad_paths=(), style=native_style())


class Ask(BaseModel):
    permission: str
    paths: list[str] = []


async def h_ask(ctx, cfg, inputs):
    answer = ctx.preflight(cfg.permission, cfg.paths)
    return NodeResult(outputs={"result": {"outcome": answer.outcome, "code": answer.code,
                                          "verdicts": [{"path": v.path, "allowed": v.allowed, "code": v.code} for v in answer.verdicts]}})


@pytest.fixture()
def registry():
    r = build_default_registry()
    r.register(NodeSpec(id="test.ask", version=1, title="ask", description="", category=Category.DATA, config_model=Ask,
                        inputs=(PortSpec(name="start", required=False),), outputs=(PortSpec(name="result", type=DataType.OBJECT),),
                        handler=h_ask, timeout_seconds=30))      # note: it declares NO permissions
    return r


@pytest.fixture()
def root(tmp_path):
    return tmp_path.resolve() / "data"


def book(root, permissions=None, **changes):
    data = {"bot_id": "fixer", "name": "Fixer", "allowed_folders": [str(root)],
            "permissions": {"fs.read": "allow", "fs.move": "allow"} if permissions is None else permissions}
    data.update(changes)
    return Rulebook.model_validate(data)


def execute(defn, registry, rulebook):
    run = Run(defn, registry, rulebook=rulebook, rulebook_version=1 if rulebook else None, constraints=CONSTRAINTS)
    asyncio.run(run.execute())
    return run


def ask(registry, rulebook, permission, paths):
    defn = WorkflowDefinition.model_validate({
        "id": "w", "name": "W", "bot_id": "fixer",
        "nodes": [{"id": "start", "type": "core.manual_trigger"},
                  {"id": "ask", "type": "test.ask", "config": {"permission": permission, "paths": paths}}],
        "edges": [{"source": "start", "source_port": "payload", "target": "ask", "target_port": "start"}]})
    run = execute(defn, registry, rulebook)
    assert run.status == RunStatus.COMPLETED, run.error
    return run.nodes["ask"].outputs["result"], run


def test_preflight_answers_per_path_without_declaring_the_permission(root, registry, tmp_path):
    inside, outside = str(root / "a.jpg"), str(tmp_path.resolve() / "elsewhere" / "b.jpg")
    answer, _ = ask(registry, book(root), "fs.move", [inside, outside])
    assert answer["outcome"] == "allow"
    assert [(v["path"], v["allowed"], v["code"]) for v in answer["verdicts"]] == [(inside, True, "ok"), (outside, False, "path.outside_allowed")]


def test_preflight_reports_ask_without_ever_pausing(root, registry):
    answer, run = ask(registry, book(root, {"fs.move": "ask"}), "fs.move", [str(root / "a.jpg")])
    assert answer["outcome"] == "ask" and answer["verdicts"][0]["allowed"] is True
    assert not [e for e in run.events.all() if e.type == EventType.APPROVAL_REQUESTED]


def test_preflight_says_deny_for_everything_when_the_permission_itself_is_refused(root, registry):
    for permission, code in (("fs.delete", "system.limit"), ("fs.write", "rulebook.not_granted"), ("fs.nonsense", "permission.unknown")):
        answer, _ = ask(registry, book(root, {"fs.read": "allow"}), permission, [str(root / "a.jpg"), str(root / "b.jpg")])
        assert answer["outcome"] == "deny" and answer["code"] == code, permission
        assert [(v["allowed"], v["code"]) for v in answer["verdicts"]] == [(False, code)] * 2


def test_preflight_without_a_rulebook_denies_everything(root, registry):
    answer, _ = ask(registry, None, "fs.read", [str(root / "a.jpg")])
    assert answer["outcome"] == "deny" and answer["code"] == "rulebook.missing" and answer["verdicts"][0]["allowed"] is False


def test_preflight_with_no_paths_just_reports_the_permission(root, registry):
    answer, _ = ask(registry, book(root, {"fs.move": "ask"}), "fs.move", [])
    assert answer["outcome"] == "ask" and answer["verdicts"] == []


def test_a_node_context_made_by_hand_denies_by_default():
    ctx = NodeContext(run_id="r", node_id="n", cancel=None)
    answer = ctx.preflight("fs.move", ["/a", "/b"])
    assert answer.outcome == "deny" and answer.code == "rulebook.missing"
    assert [v.allowed for v in answer.verdicts] == [False, False]


