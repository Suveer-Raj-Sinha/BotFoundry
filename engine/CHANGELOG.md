# BotFoundry Engine Changelog

Python engine and local API. The version lives in one place: `botfoundry/__init__.py`.
Format: [Keep a Changelog](https://keepachangelog.com). Versioning: 0.MINOR.PATCH until 1.0
(MINOR = new capability or milestone, PATCH = fix only).
Change versions with `python scripts/bump.py engine X.Y.Z` from the repo root.

## [0.4.0] - 2026-10-16

### Added
- Workflow core (`botfoundry/workflow/`), Phase 1.1:
  - Node contract (`NodeSpec`): stable id and version, typed input/output ports, Pydantic config
    schema, required permissions, timeout, cancellation token, structured result and error types.
  - Node registry with (id, version) lookup and a JSON-friendly catalog.
  - Workflow definition format (`WorkflowDefinition`) that rejects unknown fields.
  - Workflow validation that reports every problem as a structured issue with a stable code,
    severity and location, and collects the permissions a workflow needs.
  - Three built-in nodes: `core.manual_trigger`, `core.condition` (branching), `core.transform`.
- Documentation: `docs/node-contract.md`.
- 52 new tests (64 total). Validator rules were mutation-checked: disabling the cycle, type or
  missing-input checks makes tests fail.

### Not yet
- Nothing executes workflows yet (runner, events, timeouts and persistence are Phase 1.2 and 1.3).

## [0.3.0] - 2026-10-16

### Added
- `run_engine.py`: entry point for the frozen (single-file) engine executable.
- `scripts/build_engine.py`: freezes the engine with PyInstaller, smoke-tests the result
  (healthy response, expected version, wrong token returns 401) and stages it for Tauri as a sidecar.
- `build` optional dependency group: `pip install -e ".[dev,build]"` adds PyInstaller.

## [0.2.0] - 2026-10-16

### Added
- Parent watchdog: `--parent-pid` option. The engine shuts itself down when the app that
  launched it no longer exists (crash, Ctrl+C, force-kill). See `botfoundry/lifecycle.py`.
- 4 tests for process liveness and the watchdog.
- Single-source versioning: `/health` and the API metadata report `botfoundry.__version__`.
- Test that fails if the CHANGELOG and the version disagree.

### Changed
- The server now starts through `uvicorn.Server` (needed for graceful shutdown).

## [0.1.0] - 2026-10-16

### Added
- FastAPI app bound to `127.0.0.1` only.
- `GET /health`: status, engine version, Python version, `ai_available` (always false for now).
- Per-launch token authentication (`X-BotFoundry-Token`, constant-time comparison).
  Tokens shorter than 16 characters are refused at startup.
- CORS allowlist: only the Vite dev server and the Tauri webview origins.
- CLI entry point: `python -m botfoundry --port 8765`.
- 6 pytest tests (token rules, CORS allow/deny, weak-token rejection).
