# BotFoundry Engine Changelog

Python engine and local API. The version lives in one place: `botfoundry/__init__.py`.
Format: [Keep a Changelog](https://keepachangelog.com). Versioning: 0.MINOR.PATCH until 1.0
(MINOR = new capability or milestone, PATCH = fix only).
Change versions with `python scripts/bump.py engine X.Y.Z` from the repo root.

## [0.6.0] - 2026-10-18

### Added
- SQLite persistence, Phase 1.3 (`botfoundry/storage/`; see `docs/persistence.md`):
  - Thread-safe database wrapper (WAL, foreign keys, explicit all-or-nothing transactions).
  - Ordered, append-only migrations, applied atomically. A database from a newer BotFoundry is
    refused with a clear message instead of being modified.
  - Repository: save/load/list/delete workflows; list and get runs (with per-node results); events read
    with a cursor; the exact workflow snapshot each run executed.
  - `RunPersister` / `Storage.attach(run)`: writes every event and the state it implies as it happens.
    Database failures never break a run; they are logged and recorded in `persister.errors`.
  - Restart safety: at start-up, runs left unfinished by a crash are failed with `run.interrupted`
    (nodes: `node.interrupted`, or skipped) and are never resumed, so work is never executed twice.
- `--data-dir` option and `default_data_dir()` (`%LOCALAPPDATA%\BotFoundry` on Windows).
- `/health` now reports `database_schema_version`.
- A database that cannot be opened stops start-up with a clear message and exit code 2; the file is left untouched.
- 35 new tests (128 total). Persistence logic was mutation-checked (seven deliberate breakages, all caught).

### Changed
- `scripts/build_engine.py` smoke test now also starts the frozen engine with a temporary data folder
  and checks the database is ready.

## [0.5.0] - 2026-10-16

### Added
- Workflow runner, Phase 1.2 (`workflow/runner.py`: `Run`, `run_workflow`):
  - Sequential execution in dependency order (ties broken by declaration order, so runs are deterministic).
  - Branching: inactive output ports skip the nodes that depend on them (skips cascade); optional
    inputs may be inactive.
  - Per-node timeouts that really interrupt the handler.
  - Cooperative cancellation from any thread: cancellable nodes are interrupted mid-run;
    non-cancellable nodes finish first, then the run is cancelled. Cancelling the asyncio task still
    leaves the run in a terminal state.
  - Runtime checks: input types, handler result shape, undeclared output ports and output types.
  - Failure policy: the first failed node stops the run; remaining nodes are marked skipped with the
    reason recorded. Unexpected exceptions are reported as `node.unexpected_error`, never swallowed.
  - Invalid workflows never start (validation runs first and its issues are in the events).
  - Run and node state records with a JSON snapshot (`Run.snapshot()`).
- Structured events (`workflow/events.py`): every event type from the spec (the approval events are
  defined but only emitted from Phase 2), plus `node_skipped`. Per-run ids 1, 2, 3..., UTC timestamps,
  thread-safe log, cursor polling (`events.since(id)`), live listeners that cannot break a run.
  Exactly one terminal event per run.
- `types.matches_type` for runtime port-type checks.
- 29 runner tests (93 in total). Seven deliberate breakages of the runner (no timeouts, no skipping,
  no result checks, ignored cancellation, failure not stopping the run, no input checks, validation
  bypass) are each caught by the tests.

### Not yet
- Runs are kept in memory only (persistence is Phase 1.3). No API or CLI to start runs yet (Phase 1.4).

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
