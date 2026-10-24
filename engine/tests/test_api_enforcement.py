import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from botfoundry.api.app import create_app
from botfoundry.config import Settings, TOKEN_HEADER
from botfoundry.rules.constraints import SystemConstraints
from botfoundry.rules.paths import WINDOWS
from botfoundry.storage import Storage
from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.node import Category, NodeResult, NodeSpec
from botfoundry.workflow.types import PortSpec

TOKEN = "test-token-1234567890"
H = {TOKEN_HEADER: TOKEN}
DL = "C:\\Users\\Me\\Downloads"
CONSTRAINTS = SystemConstraints(protected_paths=("C:\\Windows",), too_broad_paths=("C:\\Users\\Me",), style=WINDOWS)


class MoveConfig(BaseModel):
    paths: list[str] = []


async def h_move(ctx, cfg, inputs):
    return NodeResult(outputs={"result": ctx.authorize("fs.move", cfg.paths)})


def registry():
    r = build_default_registry()
    r.register(NodeSpec(id="test.mover", version=1, title="Mover", description="", category=Category.FILES,
                        config_model=MoveConfig, inputs=(PortSpec(name="value"),), outputs=(PortSpec(name="result"),),
                        handler=h_move, permissions=frozenset({"fs.move"}), timeout_seconds=5))
    return r


def rulebook(**changes):
    data = {"bot_id": "fixer", "name": "Fixer", "allowed_folders": [DL], "permissions": {"fs.move": "ask"},
            "limits": {"max_files_per_run": 10}}
    data.update(changes)
    return data


def mover_workflow(bot_id="fixer", workflow_id="w"):
    wf = {"id": workflow_id, "name": "W", "nodes": [
        {"id": "start", "type": "core.manual_trigger", "config": {"payload": {"a": 1}}},
        {"id": "move", "type": "test.mover", "config": {"paths": [DL + "\\a.txt"]}}],
        "edges": [{"source": "start", "source_port": "payload", "target": "move", "target_port": "value"}]}
    if bot_id:
        wf["bot_id"] = bot_id
    return wf


@pytest.fixture()
def env(tmp_path):
    storage = Storage.open(tmp_path / "e.db")
    app = create_app(Settings(token=TOKEN), storage, registry(), CONSTRAINTS)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, storage
    storage.close()


def until(check, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def run_detail(client, run_id):
    return client.get(f"/runs/{run_id}", headers=H).json()


def start(client, workflow):
    r = client.post("/runs", json={"definition": workflow}, headers=H)
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


def waiting(client, run_id):
    return until(lambda: run_detail(client, run_id)["status"] == "waiting_approval" and run_detail(client, run_id))


def finished(client, run_id):
    return until(lambda: (r := run_detail(client, run_id))["status"] in ("completed", "failed", "cancelled") and r)


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/approvals", None), ("GET", "/runs/x/approvals", None),
    ("POST", "/runs/x/approvals/ap-1", {"approve": True})])
def test_approval_endpoints_require_the_token(env, method, path, body):
    client, _ = env
    assert client.request(method, path, json=body).status_code == 401


def test_permissioned_workflows_without_a_rulebook_are_refused_up_front(env):
    client, _ = env
    for workflow, expected in ((mover_workflow(bot_id=None), "no bot_id"), (mover_workflow(), "has no Rulebook")):
        r = client.post("/runs", json={"definition": workflow}, headers=H)
        assert r.status_code == 422 and r.json()["error"]["code"] == "rulebook_required"
        assert expected in r.json()["error"]["message"] and r.json()["error"]["details"]["required_permissions"] == ["fs.move"]
    assert client.get("/runs", headers=H).json() == {"runs": []}                 # nothing was created


def test_a_rulebook_that_allows_the_action_lets_the_run_proceed(env):
    client, _ = env
    client.put("/rulebooks/fixer", json=rulebook(permissions={"fs.move": "allow"}), headers=H)
    run = finished(client, start(client, mover_workflow()))
    assert run["status"] == "completed" and run["rulebook_bot_id"] == "fixer" and run["rulebook_version"] == 1
    assert [p.lower() for p in run["nodes"][1]["outputs"]["result"]] == [(DL + "\\a.txt").lower()]   # case: see test_enforcement
    assert client.get("/runs", headers=H).json()["runs"][0]["rulebook_bot_id"] == "fixer"


def test_the_full_approval_flow(env):
    client, _ = env
    client.put("/rulebooks/fixer", json=rulebook(), headers=H)
    run_id = start(client, mover_workflow())
    detail = waiting(client, run_id)
    assert detail["active"] and detail["nodes"][1]["status"] == "waiting_approval"
    [pending] = detail["pending_approvals"]
    assert pending["status"] == "pending" and pending["kind"] == "node_permissions" and "fs.move" in pending["summary"]
    inbox = client.get("/approvals", headers=H).json()["approvals"]
    assert [(a["run_id"], a["approval_id"], a["workflow_id"]) for a in inbox] == [(run_id, "ap-1", "w")]
    assert client.get(f"/runs/{run_id}/approvals", headers=H).json()["approvals"][0]["status"] == "pending"
    events = client.get(f"/runs/{run_id}/events", headers=H).json()
    assert events["events"][-1]["type"] == "approval_requested" and events["finished"] is False

    answer = client.post(f"/runs/{run_id}/approvals/ap-1", json={"approve": True, "note": "ok by me"}, headers=H)
    assert answer.status_code == 200
    assert (answer.json()["status"], answer.json()["reason"], answer.json()["note"]) == ("granted", "approved", "ok by me")
    run = finished(client, run_id)
    assert run["status"] == "completed" and run["pending_approvals"] == []
    assert client.get("/approvals", headers=H).json() == {"approvals": []}
    assert client.get(f"/runs/{run_id}/approvals", headers=H).json()["approvals"][0]["status"] == "granted"


def test_denying_fails_the_run(env):
    client, _ = env
    client.put("/rulebooks/fixer", json=rulebook(), headers=H)
    run_id = start(client, mover_workflow())
    waiting(client, run_id)
    client.post(f"/runs/{run_id}/approvals/ap-1", json={"approve": False, "note": "no"}, headers=H)
    run = finished(client, run_id)
    assert run["status"] == "failed" and run["error"]["code"] == "node.approval_denied"
    assert run["nodes"][1]["status"] == "failed"


def test_answer_errors(env):
    client, _ = env
    client.put("/rulebooks/fixer", json=rulebook(), headers=H)
    run_id = start(client, mover_workflow())
    waiting(client, run_id)
    path = f"/runs/{run_id}/approvals"
    assert client.post(f"{path}/ap-99", json={"approve": True}, headers=H).json()["error"]["code"] == "approval_not_found"
    assert client.post("/runs/ghost/approvals/ap-1", json={"approve": True}, headers=H).json()["error"]["code"] == "run_not_found"
    assert client.post(f"{path}/ap-1", json={}, headers=H).status_code == 422
    assert client.post(f"{path}/ap-1", json={"approve": True, "note": "x" * 501}, headers=H).status_code == 422
    assert client.post(f"{path}/ap-1", json={"approve": True}, headers=H).status_code == 200
    again = client.post(f"{path}/ap-1", json={"approve": False}, headers=H)
    assert again.status_code in (409,) and again.json()["error"]["code"] in ("approval_already_decided", "run_not_active")
    finished(client, run_id)
    late = client.post(f"{path}/ap-1", json={"approve": True}, headers=H)
    assert late.status_code == 409 and late.json()["error"]["code"] == "run_not_active"
    assert client.get("/runs/ghost/approvals", headers=H).status_code == 404


def test_a_run_keeps_the_rulebook_version_it_started_with(env):
    client, _ = env
    client.put("/rulebooks/fixer", json=rulebook(), headers=H)                          # v1: ask
    run_id = start(client, mover_workflow())
    waiting(client, run_id)
    client.put("/rulebooks/fixer", json=rulebook(permissions={"fs.move": "deny"}), headers=H)   # v2: deny, mid-run
    client.post(f"/runs/{run_id}/approvals/ap-1", json={"approve": True}, headers=H)
    first = finished(client, run_id)
    assert first["status"] == "completed" and first["rulebook_version"] == 1             # v1 governed this run
    second = finished(client, start(client, mover_workflow()))                            # a new run gets v2
    assert second["rulebook_version"] == 2 and second["status"] == "failed"
    assert second["nodes"][1]["error"]["code"] == "permission.denied"


def test_a_stored_rulebook_that_breaks_current_system_rules_is_not_used(env, tmp_path):
    client, storage = env
    client.put("/rulebooks/fixer", json=rulebook(), headers=H)
    stricter = SystemConstraints(protected_paths=("C:\\Users\\Me\\Downloads",), style=WINDOWS)   # Downloads is now protected
    with TestClient(create_app(Settings(token=TOKEN), storage, registry(), stricter), raise_server_exceptions=False) as strict:
        r = strict.post("/runs", json={"definition": mover_workflow()}, headers=H)
        assert r.status_code == 422 and r.json()["error"]["code"] == "rulebook_invalid"
        assert "rulebook.folder_protected" in {i["code"] for i in r.json()["error"]["details"]["issues"]}
        assert strict.get("/runs", headers=H).json() == {"runs": []}


def test_cancelling_a_run_that_is_waiting_for_approval(env):
    client, _ = env
    client.put("/rulebooks/fixer", json=rulebook(), headers=H)
    run_id = start(client, mover_workflow())
    waiting(client, run_id)
    assert client.post(f"/runs/{run_id}/cancel", headers=H).status_code == 202
    run = finished(client, run_id)
    assert run["status"] == "cancelled" and run["nodes"][1]["status"] == "cancelled"
    approval = client.get(f"/runs/{run_id}/approvals", headers=H).json()["approvals"][0]
    assert (approval["status"], approval["reason"]) == ("denied", "cancelled")
    assert client.get("/approvals", headers=H).json() == {"approvals": []}


def test_engine_shutdown_while_waiting_leaves_a_clean_record(tmp_path):
    storage = Storage.open(tmp_path / "s.db")
    app = create_app(Settings(token=TOKEN), storage, registry(), CONSTRAINTS)
    context = TestClient(app, raise_server_exceptions=False)
    client = context.__enter__()
    client.put("/rulebooks/fixer", json=rulebook(), headers=H)
    run_id = start(client, mover_workflow())
    waiting(client, run_id)
    context.__exit__(None, None, None)
    assert storage.get_run(run_id)["status"] == "cancelled"
    assert [(a["status"], a["reason"]) for a in storage.get_approvals(run_id)] == [("denied", "cancelled")]
    assert storage.mark_interrupted_runs() == []
    storage.close()


def test_workflows_without_permissions_still_record_their_rulebook(env):
    client, _ = env
    client.put("/rulebooks/fixer", json=rulebook(), headers=H)
    free = {"id": "free", "name": "Free", "bot_id": "fixer",
            "nodes": [{"id": "start", "type": "core.manual_trigger"}], "edges": []}
    run = finished(client, start(client, free))
    assert run["status"] == "completed" and run["rulebook_version"] == 1
    no_bot = {**free, "id": "free2"}
    del no_bot["bot_id"]
    assert finished(client, start(client, no_bot))["rulebook_version"] is None
