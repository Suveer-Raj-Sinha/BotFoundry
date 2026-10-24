import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from botfoundry.storage.migrations import MIGRATIONS
from botfoundry.config import DATABASE_FILE
from botfoundry.storage import Storage
from botfoundry.workflow.builtin_nodes import build_default_registry
from botfoundry.workflow.definition import WorkflowDefinition
from botfoundry.workflow.events import EventType
from botfoundry.workflow.runner import NodeStatus, Run, RunStatus

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
TOKEN = "cli-test-token-1234567890"


def cli(*args, env_extra=None, timeout=60):
    env = {k: v for k, v in os.environ.items() if k != "BOTFOUNDRY_TOKEN"}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    # stdin=DEVNULL: the child must never inherit the developer's terminal (it would see a real keyboard and
    # start asking approval questions instead of behaving as it does when run unattended).
    return subprocess.run([sys.executable, "-m", "botfoundry", *map(str, args)], capture_output=True, text=True,
                          timeout=timeout, env=env, stdin=subprocess.DEVNULL)


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


# ---- nodes / validate ---------------------------------------------------------------------
def test_nodes_lists_the_catalog():
    out = cli("nodes")
    assert out.returncode == 0
    for node_id in ("core.manual_trigger", "core.condition", "core.transform", "core.wait", "core.approval_gate"):
        assert node_id in out.stdout
    catalog = json.loads(cli("nodes", "--json").stdout)
    assert {n["id"] for n in catalog} >= {"core.wait"} and "config_schema" in catalog[0]


def test_validate_good_and_bad(tmp_path):
    assert "valid" in cli("validate", EXAMPLES / "hello.workflow.json").stdout
    bad = write(tmp_path, "bad.json", {"id": "b", "name": "B", "nodes": [{"id": "x", "type": "core.nope"}]})
    out = cli("validate", bad)
    assert out.returncode == 1 and "node.unknown_type" in out.stdout and "graph.no_trigger" in out.stdout
    as_json = json.loads(cli("validate", bad, "--json").stdout)
    assert as_json["ok"] is False


def test_validate_reports_file_problems_without_a_traceback(tmp_path):
    missing = cli("validate", tmp_path / "nope.json")
    assert missing.returncode == 2 and "File not found" in missing.stderr and "Traceback" not in missing.stderr
    broken = cli("validate", write(tmp_path, "broken.json", "{ not json"))
    assert broken.returncode == 2 and "not valid JSON" in broken.stderr


# ---- run ----------------------------------------------------------------------------------
def test_run_completes_and_saves_history(tmp_path):
    data = tmp_path / "data"
    out = cli("run", EXAMPLES / "hello.workflow.json", "--data-dir", data)
    assert out.returncode == 0, out.stderr
    assert "run_completed" in out.stdout and "Run " in out.stdout
    storage = Storage.open(data / DATABASE_FILE)
    runs = storage.list_runs()
    assert len(runs) == 1 and runs[0]["status"] == "completed" and runs[0]["workflow_id"] == "hello"
    assert storage.get_events(runs[0]["run_id"])[-1].type == EventType.RUN_COMPLETED
    storage.close()


def test_run_with_no_save_writes_nothing(tmp_path):
    data = tmp_path / "data"
    assert cli("run", EXAMPLES / "hello.workflow.json", "--no-save", "--data-dir", data).returncode == 0
    assert not data.exists()


def test_run_failure_exit_code_and_message(tmp_path):
    data = tmp_path / "data"
    out = cli("run", EXAMPLES / "failing.workflow.json", "--data-dir", data)
    assert out.returncode == 1
    assert "condition.type_error" in out.stdout and "never_runs" in out.stdout
    storage = Storage.open(data / DATABASE_FILE)
    assert storage.list_runs()[0]["status"] == "failed"
    storage.close()


def test_run_branching_skips_the_other_branch():
    out = cli("run", EXAMPLES / "branching.workflow.json", "--no-save")
    assert out.returncode == 0
    lines = {line.split()[0]: line for line in out.stdout.splitlines() if line.startswith("  ")}
    assert "completed" in lines["many"] and "skipped" in lines["few"]


def test_run_json_mode_is_machine_readable():
    out = cli("run", EXAMPLES / "hello.workflow.json", "--no-save", "--json")
    assert out.returncode == 0
    lines = [json.loads(line) for line in out.stdout.splitlines()]
    assert lines[0]["type"] == "run_created" and lines[-2]["type"] == "run_completed"
    assert lines[-1]["result"]["status"] == "completed"


def test_run_refuses_an_invalid_workflow_and_saves_nothing(tmp_path):
    data = tmp_path / "data"
    bad = write(tmp_path, "bad.json", {"id": "b", "name": "B", "nodes": [{"id": "x", "type": "core.nope"}]})
    out = cli("run", bad, "--data-dir", data)
    assert out.returncode == 1 and "nothing was run" in out.stderr and "node.unknown_type" in out.stderr
    assert not data.exists()


def test_cli_run_never_recovers_runs_belonging_to_another_process(tmp_path):
    """Only the engine process does crash recovery. A CLI run on the same data folder must leave a
    run that another (live) engine is executing alone."""
    data = tmp_path / "data"
    storage = Storage.open(data / DATABASE_FILE)
    definition = WorkflowDefinition.model_validate(json.loads((EXAMPLES / "hello.workflow.json").read_text()))
    live = Run(definition, build_default_registry())
    storage.attach(live)
    live.status = RunStatus.RUNNING
    live.nodes["start"].status = NodeStatus.RUNNING
    live.events.append(EventType.NODE_STARTED, "start")
    storage.close()

    assert cli("run", EXAMPLES / "hello.workflow.json", "--data-dir", data).returncode == 0
    storage = Storage.open(data / DATABASE_FILE)
    assert storage.get_run(live.run_id)["status"] == "running"      # untouched
    storage.close()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT delivery to a child is POSIX-only in tests")
def test_ctrl_c_cancels_a_running_workflow_and_records_it(tmp_path):
    data = tmp_path / "data"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "-m", "botfoundry", "run", str(EXAMPLES / "slow.workflow.json"),
                             "--data-dir", str(data)], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, env=env)
    try:
        deadline = time.monotonic() + 20
        for line in proc.stdout:
            if "node_progress" in line:
                break
            assert time.monotonic() < deadline, "the slow node never started"
        proc.send_signal(signal.SIGINT)
        rest, _ = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130
    assert "run_cancelled" in rest
    storage = Storage.open(data / DATABASE_FILE)
    saved = storage.list_runs()[0]
    assert saved["status"] == "cancelled"
    assert storage.get_events(saved["run_id"])[-1].type == EventType.RUN_CANCELLED
    nodes = {n["node_id"]: n["status"] for n in storage.get_run(saved["run_id"])["nodes"]}
    assert nodes == {"start": "completed", "pause": "cancelled", "after": "cancelled"}   # never "running"
    summary = [line.split() for line in rest.splitlines() if line.startswith("  ")]
    assert ["pause", "cancelled"] == next(parts for parts in summary if parts[0] == "pause")[:2]
    storage.close()


# ---- serve (the desktop app and the frozen exe use exactly this invocation) ---------------
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_still_starts_with_the_original_arguments(tmp_path):
    port = free_port()
    log = (tmp_path / "engine.log").open("w+")
    proc = subprocess.Popen([sys.executable, "-m", "botfoundry", "--port", str(port), "--data-dir", str(tmp_path / "d"),
                             "--parent-pid", str(os.getpid())], env={**os.environ, "BOTFOUNDRY_TOKEN": TOKEN},
                            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    try:
        body = None
        for _ in range(100):
            try:
                request = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers={"X-BotFoundry-Token": TOKEN})
                with urllib.request.urlopen(request, timeout=2) as response:
                    body = json.loads(response.read())
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.2)
        assert body is not None, "engine did not start"
        assert body["status"] == "ok" and body["database_schema_version"] == MIGRATIONS[-1].version
        request = urllib.request.Request(f"http://127.0.0.1:{port}/nodes", headers={"X-BotFoundry-Token": TOKEN})
        with urllib.request.urlopen(request, timeout=2) as response:
            assert len(json.loads(response.read())["nodes"]) == 5
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        log.close()


def test_serve_without_a_token_exits_cleanly(tmp_path):
    out = cli("--port", free_port(), "--data-dir", tmp_path / "d")
    assert out.returncode == 2 and "BOTFOUNDRY_TOKEN" in out.stderr and "Traceback" not in out.stderr


def test_serve_with_an_unreadable_database_exits_cleanly(tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    (data / DATABASE_FILE).write_bytes(b"not a database" * 20)
    out = cli("serve", "--port", free_port(), "--data-dir", data, env_extra={"BOTFOUNDRY_TOKEN": TOKEN})
    assert out.returncode == 2 and "cannot start" in out.stderr
    assert (data / DATABASE_FILE).read_bytes().startswith(b"not a database")     # never overwritten


def test_top_level_help_lists_the_commands():
    out = cli("--help")
    assert out.returncode == 0 and all(word in out.stdout for word in ("serve", "validate", "run", "nodes"))


# ---- rulebook commands --------------------------------------------------------------------
def test_rulebook_validate_and_check(tmp_path):
    rulebook = write(tmp_path, "r.json", {"bot_id": "fixer", "name": "Fixer", "limits": {"max_files_per_run": 5},
                                          "permissions": {"git.read": "allow", "process.run_tests": "ask"}})
    assert cli("rulebook", "validate", rulebook).returncode == 0
    allowed = cli("rulebook", "check", rulebook, "git.read")
    assert allowed.returncode == 0 and allowed.stdout.startswith("ALLOW")
    asked = cli("rulebook", "check", rulebook, "process.run_tests")
    assert asked.returncode == 3 and asked.stdout.startswith("ASK")
    denied = cli("rulebook", "check", rulebook, "fs.delete")
    assert denied.returncode == 1 and "system.limit" in denied.stdout
    absolute_here = "C:\\nowhere\\x" if os.name == "nt" else "/nowhere/x"      # absolute on this OS, but not approved
    outside = cli("rulebook", "check", rulebook, "git.read", absolute_here)
    assert outside.returncode == 1 and "path.outside_allowed" in outside.stdout
    relative = cli("rulebook", "check", rulebook, "git.read", "nowhere/x")           # relative on every OS
    assert relative.returncode == 1 and "path.not_absolute" in relative.stdout
    assert json.loads(cli("rulebook", "check", rulebook, "git.read", "--json").stdout)["outcome"] == "allow"


def test_rulebook_validate_reports_errors(tmp_path):
    bad = write(tmp_path, "bad.json", {"bot_id": "fixer", "name": "Fixer", "permissions": {"fs.delete": "allow", "fs.typo": "allow"}})
    out = cli("rulebook", "validate", bad)
    assert out.returncode == 1 and "rulebook.exceeds_system_limit" in out.stdout and "rulebook.unknown_permission" in out.stdout
    refused = cli("rulebook", "check", bad, "fs.read")
    assert refused.returncode == 1 and "fix them" in refused.stderr
    assert cli("rulebook", "validate", tmp_path / "missing.json").returncode == 2


# ---- approvals and rulebooks on the command line ------------------------------------------
def test_approval_example_with_each_answer_mode(tmp_path):
    example = EXAMPLES / "approval.workflow.json"
    approved = cli("run", example, "--no-save", "--approve", "yes")
    assert approved.returncode == 0 and "approval_granted" in approved.stdout and "run_completed" in approved.stdout
    denied = cli("run", example, "--no-save", "--approve", "no")
    assert denied.returncode == 1 and "approval_denied" in denied.stdout and "node.approval_denied" in denied.stdout
    unattended = cli("run", example, "--no-save")                    # no terminal attached: denied by default
    assert unattended.returncode == 1 and "No terminal" in unattended.stderr and "approval_denied" in unattended.stdout


def test_a_rulebook_file_is_recorded_and_checked(tmp_path):
    workflow = json.loads((EXAMPLES / "hello.workflow.json").read_text())
    workflow["bot_id"] = "fixer"
    wf_file = write(tmp_path, "w.json", workflow)
    good = write(tmp_path, "rb.json", {"bot_id": "fixer", "name": "Fixer", "permissions": {"git.read": "allow"}})
    out = cli("run", wf_file, "--no-save", "--rulebook", good)
    assert out.returncode == 0 and "Rulebook: fixer" in out.stdout
    bad = write(tmp_path, "bad.json", {"bot_id": "fixer", "name": "Fixer", "permissions": {"fs.delete": "allow"}})
    refused = cli("run", wf_file, "--no-save", "--rulebook", bad)
    assert refused.returncode == 1 and "nothing was run" in refused.stderr and "rulebook.exceeds_system_limit" in refused.stderr


def test_the_bots_saved_rulebook_is_found_automatically(tmp_path):
    from botfoundry.rules.models import Rulebook
    data = tmp_path / "data"
    storage = Storage.open(data / DATABASE_FILE)
    storage.save_rulebook(Rulebook.model_validate({"bot_id": "fixer", "name": "Fixer", "permissions": {"git.read": "allow"}}))
    storage.close()
    workflow = json.loads((EXAMPLES / "hello.workflow.json").read_text())
    workflow["bot_id"] = "fixer"
    out = cli("run", write(tmp_path, "w.json", workflow), "--data-dir", data)
    assert out.returncode == 0 and "Rulebook: fixer v1" in out.stdout
    storage = Storage.open(data / DATABASE_FILE)
    assert storage.list_runs()[0]["rulebook_version"] == 1 and storage.list_runs()[0]["rulebook_bot_id"] == "fixer"
    storage.close()


# ---- is anyone there to answer? -------------------------------------------------------------
def test_an_empty_stdin_is_never_treated_as_a_terminal(monkeypatch):
    """On Windows Python's isatty() says True for NUL; the engine must not believe it."""
    from botfoundry.cli import stdin_is_interactive
    with open(os.devnull) as empty:
        monkeypatch.setattr(sys, "stdin", empty)
        assert stdin_is_interactive() is False
    read_end, write_end = os.pipe()
    with os.fdopen(read_end) as pipe:
        monkeypatch.setattr(sys, "stdin", pipe)
        assert stdin_is_interactive() is False
    os.close(write_end)
    monkeypatch.setattr(sys, "stdin", None)
    assert stdin_is_interactive() is False


@pytest.mark.skipif(os.name == "nt", reason="needs a pseudo-terminal")
def test_a_real_terminal_is_recognized(monkeypatch):
    import pty
    from botfoundry.cli import stdin_is_interactive
    master, slave = pty.openpty()
    try:
        with os.fdopen(slave) as terminal:
            monkeypatch.setattr(sys, "stdin", terminal)
            assert stdin_is_interactive() is True
    finally:
        os.close(master)


def test_windows_nul_is_rejected_even_when_isatty_lies(monkeypatch):
    """Simulates Windows (Python 3.11): isatty() is True for NUL, but the console check says it is not a console."""
    import ctypes
    import types

    from botfoundry import cli

    class FakeStdin:
        def isatty(self):
            return True

        def fileno(self):
            return 7

    class Call:
        def __init__(self, outcome):
            self.outcome = outcome

        def __call__(self, handle, mode):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    def pretend_windows(console_result):
        kernel = types.SimpleNamespace(GetConsoleMode=Call(console_result))
        monkeypatch.setattr(cli, "os", types.SimpleNamespace(name="nt"))
        monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(get_osfhandle=lambda fd: 123))
        monkeypatch.setattr(ctypes, "WinDLL", lambda name, use_last_error=False: kernel, raising=False)
        monkeypatch.setattr(sys, "stdin", FakeStdin())

    pretend_windows(0)                                  # NUL: not a console
    assert cli.stdin_is_interactive() is False
    pretend_windows(1)                                  # a genuine console window
    assert cli.stdin_is_interactive() is True
    pretend_windows(OSError("invalid handle"))          # anything odd: unattended, never interactive
    assert cli.stdin_is_interactive() is False
