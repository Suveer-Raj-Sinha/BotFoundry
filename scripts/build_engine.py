#!/usr/bin/env python3
"""Freeze the engine into one executable, smoke-test it, and stage it for Tauri.

Run from the repo root with the ENGINE virtual environment's Python:
    engine\\.venv\\Scripts\\python.exe scripts\\build_engine.py
Options: --skip-smoke   build and stage without running the smoke test
"""
import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
BUILD = ENGINE / "build"
BIN_DIR = ROOT / "apps" / "desktop" / "src-tauri" / "binaries"
NAME = "botfoundry-engine"
EXT = ".exe" if os.name == "nt" else ""

sys.path.insert(0, str(ENGINE))
from botfoundry import __version__  # noqa: E402
from botfoundry.storage.migrations import MIGRATIONS  # noqa: E402


def host_triple() -> str:
    out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("Could not read the Rust host triple from `rustc -vV`.")


def freeze() -> Path:
    print(f"[1/3] Freezing engine {__version__} with PyInstaller (this takes a minute)...")
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--name", NAME,
            "--distpath", str(BUILD / "dist"),
            "--workpath", str(BUILD / "work"),
            "--specpath", str(BUILD),
            "--paths", str(ENGINE),
            "--collect-submodules", "uvicorn",
            "--collect-submodules", "botfoundry",
            str(ENGINE / "run_engine.py"),
        ],
        check=True,
        cwd=ENGINE,
    )
    exe = BUILD / "dist" / (NAME + EXT)
    if not exe.exists():
        raise SystemExit(f"PyInstaller finished but {exe} does not exist.")
    print(f"      built {exe} ({exe.stat().st_size / 1_000_000:.1f} MB)")
    return exe


def stop(proc: subprocess.Popen) -> None:
    if os.name == "nt":  # onefile has a launcher + child; end the whole tree
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def http_get(url: str, token: str | None) -> tuple[int, str]:
    return http_call("GET", url, token)


def http_call(method: str, url: str, token: str | None, body: object = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["X-BotFoundry-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, ""


HELLO = {"id": "smoke", "name": "Smoke test", "nodes": [
    {"id": "start", "type": "core.manual_trigger", "config": {"payload": {"ok": True}}},
    {"id": "text", "type": "core.transform", "config": {"operation": "to_string"}}],
    "edges": [{"source": "start", "source_port": "payload", "target": "text", "target_port": "value"}]}


def check_a_rulebook(base: str, token: str) -> None:
    """Save a Rulebook and ask the frozen engine to judge actions against it."""
    book = {"bot_id": "smoke-bot", "name": "Smoke", "permissions": {"git.read": "allow", "process.run_tests": "allow"}}
    status, body = http_call("PUT", f"{base}/rulebooks/smoke-bot", token, book)
    if status != 422:  # process.run_tests: allow exceeds the system limit, so this must be refused
        raise SystemExit(f"Smoke test FAILED: an over-permissive Rulebook was not refused ({status})")
    book["permissions"] = {"git.read": "allow", "process.run_tests": "ask"}
    status, body = http_call("PUT", f"{base}/rulebooks/smoke-bot", token, book)
    if status != 200 or json.loads(body)["version"] != 1:
        raise SystemExit(f"Smoke test FAILED: saving a Rulebook returned {status}: {body}")
    for permission, expected in (("git.read", "allow"), ("process.run_tests", "ask"), ("fs.delete", "deny")):
        status, body = http_call("POST", f"{base}/rulebooks/smoke-bot/evaluate", token, {"permission": permission})
        if status != 200 or json.loads(body)["decision"]["outcome"] != expected:
            raise SystemExit(f"Smoke test FAILED: {permission} should be {expected}: {status} {body}")


def run_a_workflow(base: str, token: str) -> None:
    """End to end through the frozen executable: start a run, wait for it, read its results back."""
    status, body = http_call("POST", f"{base}/runs", token, {"definition": HELLO})
    if status != 202:
        raise SystemExit(f"Smoke test FAILED: starting a run returned {status}: {body}")
    run_id = json.loads(body)["run_id"]
    for _ in range(100):
        status, body = http_call("GET", f"{base}/runs/{run_id}", token)
        run = json.loads(body)
        if run["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.1)
    if run["status"] != "completed" or run["nodes"][1]["outputs"] != {"result": '{"ok": true}'}:
        raise SystemExit(f"Smoke test FAILED: the run did not complete correctly: {run}")
    status, body = http_call("GET", f"{base}/runs/{run_id}/events", token)
    if json.loads(body)["events"][-1]["type"] != "run_completed":
        raise SystemExit("Smoke test FAILED: the saved events do not end with run_completed")


def smoke_test(exe: Path) -> None:
    print("[2/3] Smoke-testing the frozen executable...")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    token = secrets.token_hex(16)
    data_dir = Path(tempfile.mkdtemp(prefix="botfoundry-smoke-"))
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    started = time.time()
    proc = subprocess.Popen(
        [str(exe), "--port", str(port), "--data-dir", str(data_dir), "--parent-pid", str(os.getpid())],
        env={**os.environ, "BOTFOUNDRY_TOKEN": token},
        stdout=log, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}/health"
    try:
        status, body = 0, ""
        while time.time() - started < 60:
            if proc.poll() is not None:
                break
            try:
                status, body = http_get(url, token)
                if status == 200:
                    break
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            time.sleep(0.3)
        elapsed = time.time() - started
        if status != 200:
            log.flush(); log.seek(0)
            raise SystemExit(f"Smoke test FAILED: no healthy response.\n--- engine output ---\n{log.read()[-2000:]}")
        if f'"engine_version":"{__version__}"' not in body:
            raise SystemExit(f"Smoke test FAILED: unexpected version in {body}")
        if f'"database_schema_version":{MIGRATIONS[-1].version}' not in body:
            raise SystemExit(f"Smoke test FAILED: database not ready in {body}")
        bad_status, _ = http_get(url, "wrong-token")
        if bad_status != 401:
            raise SystemExit(f"Smoke test FAILED: wrong token returned {bad_status}, expected 401")
        run_a_workflow(f"http://127.0.0.1:{port}", token)
        check_a_rulebook(f"http://127.0.0.1:{port}", token)
        print(f"      OK: healthy after {elapsed:.1f}s, version {__version__}, database schema "
              f"v{MIGRATIONS[-1].version}, wrong token -> 401, a workflow ran end to end, Rulebook limits enforced")
    finally:
        stop(proc)
        log.close()
        shutil.rmtree(data_dir, ignore_errors=True)


def stage(exe: Path) -> Path:
    print("[3/3] Staging for Tauri...")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = BIN_DIR / f"{NAME}-{host_triple()}{EXT}"
    shutil.copy2(exe, target)
    print(f"      {target}")
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-smoke", action="store_true")
    args = parser.parse_args()
    exe = freeze()
    if not args.skip_smoke:
        smoke_test(exe)
    stage(exe)
    print("Done.")


if __name__ == "__main__":
    main()
