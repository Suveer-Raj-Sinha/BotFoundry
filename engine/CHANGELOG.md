# BotFoundry Engine Changelog

Python engine and local API. The version lives in one place: `botfoundry/__init__.py`.
Format: [Keep a Changelog](https://keepachangelog.com). Versioning: 0.MINOR.PATCH until 1.0
(MINOR = new capability or milestone, PATCH = fix only).
Change versions with `python scripts/bump.py engine X.Y.Z` from the repo root.

## [0.7.1] - 2026-10-20

### Fixed
- A run cancelled from outside (Ctrl+C in the CLI, or the forced-shutdown fallback) left the node that was
  running marked `running` in its final state, in memory and in the saved history, although the run itself was
  correctly `cancelled`. Found by running the CLI on Windows. That node is now `cancelled` with the reason
  "Cancelled while running", and the `run_cancelled` event names it in `running_node`. The API cancel route was
  not affected.
- Defensive invariant: when any run ends, a node can no longer be left `running`. If the engine itself fails
  mid-node, that node is recorded as failed with code `node.aborted`.

### Tests
- 3 new tests (192 total): cancellation by task, persisted state and the CLI Ctrl+C test now check every
  node's status, not only the run's. A test checks that no finished run leaves a node running or pending.
  Reverting the fix makes five of them fail.

## [0.7.0] - 2026-10-18

### Added
- Local API, Phase 1.4 (`docs/api.md`): `GET /nodes`; `POST /workflows/validate`; workflow list, get, save
  (`PUT`) and delete; `POST /runs` (saved workflow or inline definition; validates first, answers 202 and runs
  in the background); run list and detail; `GET /runs/{id}/events` with a cursor; `POST /runs/{id}/cancel`.
  All behind the token. Starting is refused for invalid workflows (422 with the issues, no run created), when
  8 runs are already active (429), or when the database cannot record the run (500, nothing started).
- Run manager (`run_manager.py`): background runs, cancel by id, history problems exposed as
  `persistence_errors`, and on engine shutdown every active run is cancelled and recorded promptly.
- Command line (`cli.py`): `nodes`, `validate`, `run` (live events, `--json`, `--no-save`, Ctrl+C cancels and
  records, exit codes 0/1/2/130) alongside the unchanged default start-up used by the desktop app.
- `core.wait` node: pauses 0 to 60 s with progress events; cancellable.
- `examples/`: hello, branching, failing and slow workflows (tested).
- 61 new tests (189 total). Nine deliberate breakages of the API, run manager and CLI were checked; two
  initially slipped through, which led to stronger tests for prompt shutdown and for never echoing submitted values.

### Changed
- **Error format:** all API errors, including 401, are now `{"error": {"code", "message", "details"}}` instead
  of `{"detail": ...}`. Status codes are unchanged.
- `create_app(settings, storage=None, registry=None)`; the desktop app's launch arguments are unchanged.
- `scripts/build_engine.py` smoke test now runs a workflow end to end through the frozen executable.
- `run_engine.py` passes the exit code through (the packaged engine now reports failures correctly).

### Notes
- Run only one engine per data folder; there is no lock yet. The CLI `run` command never performs crash
  recovery, so it cannot interfere with a live engine.

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
