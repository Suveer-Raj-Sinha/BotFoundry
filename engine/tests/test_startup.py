import asyncio
import sys

from fastapi.testclient import TestClient

from botfoundry.api.app import create_app
from botfoundry.config import DATABASE_FILE, Settings, TOKEN_HEADER, default_data_dir
from botfoundry.startup import open_storage
from botfoundry.storage import Storage
from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.definition import WorkflowDefinition
from botfoundry.workflow.events import EventType
from botfoundry.workflow.runner import NodeStatus, Run, RunStatus

TOKEN = "test-token-1234567890"


def test_health_reports_database_schema_version(tmp_path):
    storage, _ = open_storage(tmp_path)
    client = TestClient(create_app(Settings(token=TOKEN), storage))
    body = client.get("/health", headers={TOKEN_HEADER: TOKEN}).json()
    assert body["database_schema_version"] == storage.schema_version == 1
    storage.close()


def test_health_without_storage_reports_none():
    client = TestClient(create_app(Settings(token=TOKEN)))
    assert client.get("/health", headers={TOKEN_HEADER: TOKEN}).json()["database_schema_version"] is None


def test_open_storage_creates_the_database_file(tmp_path):
    folder = tmp_path / "new" / "folder"
    storage, interrupted = open_storage(folder)
    assert (folder / DATABASE_FILE).exists() and interrupted == []
    storage.close()


def test_startup_recovers_runs_left_unfinished_by_a_crash(tmp_path):
    first = Storage.open(tmp_path / DATABASE_FILE)
    definition = WorkflowDefinition.model_validate({
        "id": "w", "name": "W", "nodes": [{"id": "start", "type": "core.manual_trigger"}], "edges": []})
    run = Run(definition, build_default_registry())
    first.attach(run)
    run.status = RunStatus.RUNNING
    run.nodes["start"].status = NodeStatus.RUNNING
    run.events.append(EventType.NODE_STARTED, "start")
    first.close()                                    # the app dies here

    storage, interrupted = open_storage(tmp_path)    # next launch
    assert interrupted == [run.run_id]
    assert storage.get_run(run.run_id)["error"]["code"] == "run.interrupted"
    storage.close()


def test_default_data_dir_uses_the_platform_location(monkeypatch, tmp_path):
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert default_data_dir() == tmp_path / "BotFoundry"
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert default_data_dir() == tmp_path / "botfoundry"
