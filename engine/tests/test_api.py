import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from botfoundry.api.app import create_app
from botfoundry.config import Settings, TOKEN_HEADER
from botfoundry.run_manager import RunManager
from botfoundry.storage import Storage
from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.definition import WorkflowDefinition
from botfoundry.workflow.node import Category, NodeResult, NodeSpec
from botfoundry.workflow.types import PortSpec

TOKEN = "test-token-1234567890"
H = {TOKEN_HEADER: TOKEN}


def node(node_id, node_type, **config):
    return {"id": node_id, "type": node_type, "config": config}


def edge(source, source_port, target, target_port="value"):
    return {"source": source, "source_port": source_port, "target": target, "target_port": target_port}


def workflow(workflow_id="w1", name="Workflow", nodes=None, edges=None):
    return {"id": workflow_id, "name": name,
            "nodes": nodes or [node("start", "core.manual_trigger", payload={"k": "v"}),
                               node("x", "core.transform", operation="to_string")],
            "edges": edges if edges is not None else [edge("start", "payload", "x")]}


def failing_workflow(workflow_id="bad"):
    return workflow(workflow_id, nodes=[node("start", "core.manual_trigger", payload={"a": 1}),
                                        node("cmp", "core.condition", operator="greater_than", compare_to=3)],
                    edges=[edge("start", "payload", "cmp")])


def slow_workflow(workflow_id="slow", seconds=10):
    return workflow(workflow_id, nodes=[node("start", "core.manual_trigger", payload={"k": 1}),
                                        node("pause", "core.wait", seconds=seconds),
                                        node("after", "core.transform", operation="to_string")],
                    edges=[edge("start", "payload", "pause"), edge("pause", "result", "after")])


@pytest.fixture()
def env(tmp_path):
    storage = Storage.open(tmp_path / "api.db")
    app = create_app(Settings(token=TOKEN), storage, build_default_registry())
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, storage, app
    storage.close()


def wait_until(check, timeout=8.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def finished_run(client, run_id):
    return wait_until(lambda: (r := client.get(f"/runs/{run_id}", headers=H).json())["status"]
                      in ("completed", "failed", "cancelled") and r)


def start(client, **body):
    response = client.post("/runs", json=body, headers=H)
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


# ---- auth, errors, CORS -------------------------------------------------------------------
ENDPOINTS = [
    ("GET", "/nodes", None), ("POST", "/workflows/validate", {}), ("GET", "/workflows", None),
    ("GET", "/workflows/w1", None), ("PUT", "/workflows/w1", {}), ("DELETE", "/workflows/w1", None),
    ("POST", "/runs", {"workflow_id": "w1"}), ("GET", "/runs", None), ("GET", "/runs/abc", None),
    ("GET", "/runs/abc/events", None), ("POST", "/runs/abc/cancel", None), ("GET", "/health", None),
]


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_every_endpoint_requires_the_token(env, method, path, body):
    client, _, _ = env
    for headers in ({}, {TOKEN_HEADER: "wrong"}):
        r = client.request(method, path, json=body, headers=headers)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"


def test_errors_use_one_format(env):
    client, _, _ = env
    assert client.get("/nope", headers=H).json()["error"]["code"] == "not_found"
    assert client.put("/nodes", headers=H).status_code == 405
    bad = client.post("/runs", json={"workflow_id": {"inner": "do-not-echo"}}, headers=H)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
    assert "do-not-echo" not in bad.text                      # submitted values are never echoed back


def test_cors_preflight_allows_the_new_methods_from_the_app(env):
    client, _, _ = env
    r = client.options("/workflows/w1", headers={"Origin": "http://tauri.localhost",
                                                 "Access-Control-Request-Method": "PUT",
                                                 "Access-Control-Request-Headers": TOKEN_HEADER})
    assert r.status_code == 200 and "PUT" in r.headers["access-control-allow-methods"]


def test_unexpected_errors_do_not_leak_details(tmp_path):
    storage = Storage.open(tmp_path / "x.db")
    app = create_app(Settings(token=TOKEN), storage)

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret internal detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/boom")                                # (no token needed for this test-only route)
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    assert "secret internal detail" not in r.text
    storage.close()


def test_engine_without_a_database_still_serves_nodes_and_health():
    with TestClient(create_app(Settings(token=TOKEN)), raise_server_exceptions=False) as client:
        assert client.get("/nodes", headers=H).status_code == 200
        assert client.get("/health", headers=H).status_code == 200
        for method, path in (("GET", "/workflows"), ("GET", "/runs")):
            r = client.request(method, path, headers=H)
            assert r.status_code == 503 and r.json()["error"]["code"] == "storage_unavailable"


# ---- nodes and validation -----------------------------------------------------------------
def test_node_catalog(env):
    client, _, _ = env
    nodes = {n["id"]: n for n in client.get("/nodes", headers=H).json()["nodes"]}
    assert {"core.manual_trigger", "core.condition", "core.transform", "core.wait", "core.approval_gate",
            "files.scan_folder", "files.hash", "files.filter", "files.find_duplicates"} <= set(nodes)
    assert "operator" in nodes["core.condition"]["config_schema"]["properties"]
    assert nodes["core.wait"]["outputs"][0]["name"] == "result"


def test_validate_endpoint(env):
    client, _, _ = env
    good = client.post("/workflows/validate", json=workflow(), headers=H)
    assert good.status_code == 200 and good.json()["ok"] is True
    bad = client.post("/workflows/validate", json=workflow(nodes=[node("x", "core.nope")], edges=[]), headers=H)
    assert bad.status_code == 200 and bad.json()["ok"] is False
    assert {"node.unknown_type", "graph.no_trigger"} <= {i["code"] for i in bad.json()["issues"]}
    broken = client.post("/workflows/validate", json={"id": "x"}, headers=H)
    assert broken.status_code == 200 and broken.json()["issues"][0]["code"] == "schema.invalid"
    assert client.post("/workflows/validate", json=[1, 2], headers=H).status_code == 422


# ---- workflows ----------------------------------------------------------------------------
def test_workflow_crud(env):
    client, _, _ = env
    assert client.get("/workflows", headers=H).json() == {"workflows": []}
    saved = client.put("/workflows/w1", json=workflow(), headers=H)
    assert saved.status_code == 200
    assert saved.json()["workflow"]["node_count"] == 2 and saved.json()["validation"]["ok"] is True
    assert client.get("/workflows/w1", headers=H).json()["name"] == "Workflow"
    client.put("/workflows/w1", json=workflow(name="Renamed"), headers=H)
    listed = client.get("/workflows", headers=H).json()["workflows"]
    assert [w["name"] for w in listed] == ["Renamed"]
    assert client.delete("/workflows/w1", headers=H).json() == {"deleted": True}
    assert client.get("/workflows/w1", headers=H).status_code == 404
    assert client.delete("/workflows/w1", headers=H).json()["error"]["code"] == "workflow_not_found"


def test_saving_validates_structure_but_keeps_work_in_progress(env):
    client, _, _ = env
    mismatch = client.put("/workflows/other", json=workflow("w1"), headers=H)
    assert mismatch.status_code == 422 and mismatch.json()["error"]["code"] == "id_mismatch"
    broken = client.put("/workflows/w1", json={"id": "w1", "name": "", "nodes": 3}, headers=H)
    assert broken.status_code == 422 and broken.json()["error"]["code"] == "workflow_invalid_structure"
    draft = workflow(nodes=[node("lonely", "core.transform", operation="to_upper")], edges=[])
    kept = client.put("/workflows/w1", json=draft, headers=H)
    assert kept.status_code == 200 and kept.json()["validation"]["ok"] is False   # saved, but flagged
    assert client.get("/workflows/w1", headers=H).status_code == 200


# ---- runs ---------------------------------------------------------------------------------
def test_run_a_saved_workflow_end_to_end(env):
    client, _, _ = env
    client.put("/workflows/w1", json=workflow(), headers=H)
    first = client.post("/runs", json={"workflow_id": "w1"}, headers=H)
    assert first.status_code == 202 and first.json()["status"] in ("created", "validating", "running", "completed")
    run = finished_run(client, first.json()["run_id"])
    assert run["status"] == "completed" and run["active"] is False and run["persistence_errors"] == []
    assert {n["node_id"]: n["status"] for n in run["nodes"]} == {"start": "completed", "x": "completed"}
    assert run["nodes"][1]["outputs"] == {"result": '{"k": "v"}'}


def test_run_an_inline_workflow_without_saving_it(env):
    client, _, _ = env
    run = finished_run(client, start(client, definition=workflow("inline")))
    assert run["workflow_id"] == "inline" and run["status"] == "completed"
    assert client.get("/workflows", headers=H).json() == {"workflows": []}


def test_invalid_workflows_are_rejected_and_no_run_is_created(env):
    client, _, _ = env
    r = client.post("/runs", json={"definition": workflow(nodes=[node("x", "core.nope")], edges=[])}, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "workflow_invalid"
    assert {"node.unknown_type", "graph.no_trigger"} <= {i["code"] for i in r.json()["error"]["details"]["issues"]}
    client.put("/workflows/draft", json=workflow("draft", nodes=[node("a", "core.transform", operation="to_upper")],
                                                 edges=[]), headers=H)
    assert client.post("/runs", json={"workflow_id": "draft"}, headers=H).status_code == 422
    assert client.get("/runs", headers=H).json() == {"runs": []}


@pytest.mark.parametrize("body", [{}, {"workflow_id": "a", "definition": {}}])
def test_start_needs_exactly_one_source(env, body):
    client, _, _ = env
    r = client.post("/runs", json=body, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


def test_start_unknown_workflow_is_404(env):
    client, _, _ = env
    r = client.post("/runs", json={"workflow_id": "ghost"}, headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == "workflow_not_found"


def test_a_run_that_fails_is_reported_as_failed(env):
    client, _, _ = env
    run = finished_run(client, start(client, definition=failing_workflow()))
    assert run["status"] == "failed" and run["error"]["code"] == "condition.type_error"
    assert {n["node_id"]: n["status"] for n in run["nodes"]}["cmp"] == "failed"


def test_events_paging_and_finish_flag(env):
    client, _, _ = env
    run_id = start(client, definition=workflow())
    finished_run(client, run_id)
    everything = client.get(f"/runs/{run_id}/events", headers=H).json()
    ids = [e["id"] for e in everything["events"]]
    assert ids == list(range(1, len(ids) + 1)) and everything["finished"] is True
    assert everything["has_more"] is False and everything["next_after"] == ids[-1]
    assert everything["events"][-1]["type"] == "run_completed"

    page = client.get(f"/runs/{run_id}/events?limit=3", headers=H).json()
    assert [e["id"] for e in page["events"]] == [1, 2, 3] and page["has_more"] is True and page["next_after"] == 3
    rest = client.get(f"/runs/{run_id}/events?after=3&limit=500", headers=H).json()
    assert [e["id"] for e in rest["events"]] == ids[3:] and rest["has_more"] is False
    empty = client.get(f"/runs/{run_id}/events?after={ids[-1]}", headers=H).json()
    assert empty["events"] == [] and empty["next_after"] == ids[-1]
    assert client.get("/runs/nope/events", headers=H).status_code == 404
    assert client.get(f"/runs/{run_id}/events?limit=0", headers=H).status_code == 422


def test_following_a_live_run_by_polling(env):
    client, _, _ = env
    run_id = start(client, definition=slow_workflow(seconds=1))
    seen, cursor = [], 0
    for _ in range(100):
        page = client.get(f"/runs/{run_id}/events?after={cursor}", headers=H).json()
        seen += page["events"]
        cursor = page["next_after"]
        if page["finished"] and not page["has_more"]:
            break
        time.sleep(0.1)
    assert seen[-1]["type"] == "run_completed"
    assert [e["id"] for e in seen] == list(range(1, len(seen) + 1))     # nothing missed, nothing repeated
    assert any(e["type"] == "node_progress" for e in seen)


def test_cancel_a_running_run(env):
    client, storage, _ = env
    run_id = start(client, definition=slow_workflow())
    wait_until(lambda: any(e["type"] == "node_started" and e["node_id"] == "pause"
                           for e in client.get(f"/runs/{run_id}/events", headers=H).json()["events"]))
    r = client.post(f"/runs/{run_id}/cancel", headers=H)
    assert r.status_code == 202 and r.json() == {"run_id": run_id, "cancel_requested": True}
    run = finished_run(client, run_id)
    assert run["status"] == "cancelled"
    assert {n["node_id"]: n["status"] for n in run["nodes"]} == {
        "start": "completed", "pause": "cancelled", "after": "cancelled"}
    again = client.post(f"/runs/{run_id}/cancel", headers=H)
    assert again.status_code == 409 and again.json()["error"]["code"] == "run_not_active"
    assert client.post("/runs/ghost/cancel", headers=H).status_code == 404


def test_cancelling_a_finished_run_is_a_conflict(env):
    client, _, _ = env
    run_id = start(client, definition=workflow())
    finished_run(client, run_id)
    assert client.post(f"/runs/{run_id}/cancel", headers=H).status_code == 409


def test_run_listing_filters_and_paging(env):
    client, _, _ = env
    ids = [start(client, definition=workflow("a")) for _ in range(2)] + [start(client, definition=workflow("b"))]
    for run_id in ids:
        finished_run(client, run_id)
    assert len(client.get("/runs", headers=H).json()["runs"]) == 3
    assert len(client.get("/runs?workflow_id=a", headers=H).json()["runs"]) == 2
    assert len(client.get("/runs?limit=2", headers=H).json()["runs"]) == 2
    assert len(client.get("/runs?limit=2&offset=2", headers=H).json()["runs"]) == 1
    assert client.get("/runs?limit=0", headers=H).status_code == 422


def test_too_many_active_runs_is_refused(env):
    client, _, app = env
    app.state.run_manager.max_active = 1
    first = start(client, definition=slow_workflow())
    r = client.post("/runs", json={"definition": slow_workflow("second")}, headers=H)
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_active_runs"
    client.post(f"/runs/{first}/cancel", headers=H)
    finished_run(client, first)


def test_database_failure_when_starting_means_no_run_is_started(env):
    client, storage, _ = env
    storage.close()
    r = client.post("/runs", json={"definition": workflow()}, headers=H)
    assert r.status_code == 500 and r.json()["error"]["code"] == "storage_error"


def test_engine_shutdown_ends_active_runs_in_a_recorded_state(tmp_path):
    storage = Storage.open(tmp_path / "shutdown.db")
    app = create_app(Settings(token=TOKEN), storage)
    context = TestClient(app, raise_server_exceptions=False)
    client = context.__enter__()
    run_id = start(client, definition=slow_workflow())
    wait_until(lambda: any(e["type"] == "node_started" and e["node_id"] == "pause"
                           for e in client.get(f"/runs/{run_id}/events", headers=H).json()["events"]))
    started = time.monotonic()
    context.__exit__(None, None, None)                            # engine shuts down while the run is active
    assert time.monotonic() - started < 3, "shutdown should cancel active runs promptly, not wait them out"
    saved = storage.get_run(run_id)
    assert saved["status"] == "cancelled"
    assert storage.get_events(run_id)[-1].type.value == "run_cancelled"
    assert storage.mark_interrupted_runs() == []                  # nothing left over for the next start-up
    storage.close()


# ---- run manager on its own ---------------------------------------------------------------
def test_manager_reports_history_problems_without_breaking_the_run(tmp_path):
    storage = Storage.open(tmp_path / "m.db")
    definition = WorkflowDefinition.model_validate(slow_workflow(seconds=0.4))
    manager = RunManager(build_default_registry(), storage)

    async def main():
        run = manager.start(definition)
        await asyncio.sleep(0.05)
        storage.close()                                           # the database disappears mid-run
        while manager.is_active(run.run_id):
            await asyncio.sleep(0.05)
        return run

    run = asyncio.run(main())
    assert run.status.value == "completed"                        # the run itself was not harmed
    assert manager.persistence_errors(run.run_id)                 # but the gap in history is visible
