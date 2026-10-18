# BotFoundry — Project Handoff

Purpose of this file: let you (or any AI assistant) continue the project from exactly where it stands,
with no access to the earlier chat. Read it top to bottom once, then use sections 8 and 12.

Last updated: 2026-10-18. Engine 0.6.0, app 0.5.0.

---

## 1. What BotFoundry is

A local-first Windows desktop automation platform: users build bots as typed node workflows, give each bot
a versioned Rulebook (permissions, action policies, approvals), and watch every action execute locally.
Tagline: "Your tools. Your bots. Your machine. Real work."

The full product spec is **Plan.docx** ("BotFoundry — Master Project Prompt"). It is the source of truth.
Keep it and give it to any new assistant. This file summarizes it and records everything decided or built since.

Principles that drive every decision (from the spec):
- Local-first. No cloud account, no telemetry, works offline, works with AI disabled.
- The Python engine enforces permissions and policies itself. The UI is never the authority.
- Never invent results. Failures are reported honestly. Never claim something works unless it was run.
- Dry-run/preview before changes, approval for sensitive actions, never silently overwrite, no auto-delete in the MVP.
- Registered, typed operations only. No unrestricted shell. AI output is never trusted without validation.
- Rule precedence: system safety constraints > explicit user permissions/prohibitions > bot Rulebook >
  workflow configuration > AI suggestions. Record the Rulebook version used for each run.

Stack (from the spec): Tauri (shell), React + TypeScript + Vite (UI), React Flow (workflow editor, later),
Python + FastAPI + Pydantic (engine), SQLite, pytest, Vitest, optional Ollama (later), Git.
Out of scope for the MVP: cloud, accounts, marketplace, multi-agent, loops/parallel execution, unrestricted
shell, autonomous deletion, remote access.

Bots: **The Fixer** (file organizer, the MVP), The Builder (developer automation), The Archivist
(personal knowledge). Screens: Workshop (home), Foundry (editor), Runner (live), Rulebook, Ledger (history).

---

## 2. Working agreement (how to help me)

- I know Python and React. I do **not** know Rust. Rust is only the thin Tauri shell; write and explain it.
- Small, testable steps. Say which files change and why. One milestone at a time.
- Give exact **PowerShell** commands. I copy files into place myself, so list each file and its destination
  folder instead of giving extract commands.
- Never claim to have run, built or tested something you did not. State what is unverified (the assistant
  could not run Windows/Tauri builds; I run them and paste back results).
- Every change that adds or alters behavior gets a changelog entry and a version bump (section 6, Versioning).
- Every step comes with a **git commit plan** (several small, logical commits with exact `git add <paths>` commands and
  conventional messages) and an updated `.gitignore` whenever new generated files, data or secrets appear.
  The repo is going on GitHub; see `docs/GIT_WORKFLOW.md`. Never suggest empty or padding commits.
- Run tests and the version check before handing files over.
- Don't skip ahead; finish and test each phase first. Propose architecture changes before making them.

---

## 3. Environment (my machine)

- Windows, PowerShell inside VS Code. Repo: `D:\Projects\BotFoundry\botfoundry` (git, branch `master`).
- **Python:** the engine venv is `engine\.venv`, created with `py -3.11` (Python 3.11.9).
  **Always call it explicitly:** `.\engine\.venv\Scripts\python.exe`. The bare `python` on my PATH is MSYS2's
  Python 3.14.3 (`C:\msys64\mingw64\bin`), which has no pip and builds venvs with a different layout.
- Node v24.20.0, npm 11.12.0, Git 2.55.0, Rust/Cargo 1.99.0 (host `x86_64-pc-windows-msvc`).
- Tauri 2 (scaffold from `npm create tauri-app`, React + TypeScript, identifier `com.botfoundry.app`,
  Rust edition 2024). Vite dev server port **1420**. Vitest 5.x.
- Installed on my machine at last run: FastAPI 0.143.2, Pydantic 2.14.1, Starlette 1.8.0, uvicorn 0.54.0, pytest 9.1.1.
- Not yet needed/used: PyInstaller is installed via `pip install -e "engine[dev,build]"`.

---

## 4. Current status

| Phase | Scope | State |
|---|---|---|
| 0a | Engine skeleton: `/health`, token auth, CORS allowlist, tests | Done, verified on my machine |
| 0b | Tauri + React shell, status screen, Rust launcher, parent watchdog, frozen engine, packaged build | Done, I confirmed every step passed on my machine |
| 1.1 | Node contract, registry, workflow format, validation, 3 built-in nodes | Delivered (64 tests in the assistant's sandbox) |
| 1.2 | Runner: sequential execution, branching, events, timeouts, cancellation, error results | Delivered (93 tests in the assistant's sandbox) |
| 1.3 | SQLite + migrations, data-directory setting, persisted runs/events, restart safety | **Delivered; 128 engine tests pass in the assistant's sandbox (Linux), and the Rust launcher and frozen engine were exercised there. I have not yet confirmed 1.1, 1.2 or 1.3 on Windows.** First action: run the tests, then commit using the commit plan in the step 1.3 instructions. |
| 1.4 | Local API (validate/run/events/cancel) + CLI | Planned |

Tests: engine 128 (pytest), app 6 (Vitest). Versions: engine 0.6.0, app 0.5.0.

**Decision made for 1.2:** the first failed node stops the run and later nodes are marked skipped with a reason
(simple and predictable; revisit only if needed). Per-item "skip and report" behavior for the Fixer happens
inside a node, not at the workflow level.

---

## 5. Roadmap (revised plan; replaces the spec's phase numbering but keeps all its requirements)

0. Foundation and packaging spike (done).
1. Engine core as a pure Python package + CLI: node contract, registry, validation (1.1), runner/events/timeouts/
   cancellation (1.2), SQLite persistence (1.3), API + CLI (1.4).
2. Rulebook: schema, versions, path authorization (Windows edge cases: junctions, symlinks, long paths, case,
   reserved names), action policies, approval gates, conflict detection, enforcement tests.
3. The Fixer: scan, filter, hash, duplicates, plan, preview, approval, safe moves, recovery log, narrow undo,
   reports, end-to-end tests on temp directories. Dogfood on a copy of my Downloads folder.
4. UI shell + **read-only** canvas: Workshop, live Runner, Ledger; Pydantic -> JSON Schema -> TypeScript pipeline
   (output folder reserved: `shared/workflow-schemas/`), CI check that generated types are current.
5. Editable canvas (React Flow), node inspector, Rulebook UI (simple / visual rules / JSON-YAML), onboarding, examples.
6. Polish, scheduled local runs, Windows installer (MSI/NSIS), clean-machine test, demo script, docs.
7. The Builder, optional local AI (Ollama), workflow suggestion with validation, templates, optional web UI.

Boundaries decided:
- **Rulebook = what is allowed** (permissions, approved folders, action policies, limits, failure behavior).
  **Workflow = what to do** (steps, filters, classification, branching). Fixer classification rules live in
  workflow node config, not in the Rulebook.
- **Undo is narrow:** only undo a move if the destination still has the same hash and the source path is free;
  otherwise explain exactly why it cannot be undone. Cross-drive moves = copy, verify hash, delete source, as one logged unit.
- Never overwrite: on a destination collision, skip and report.

Spec acceptance criteria (section 16 of Plan.docx) are the MVP definition of done. Do not claim any until verified.

---

## 6. Architecture and key decisions (with reasons)

**Runtime layout**
```
Tauri window (React UI) --HTTP + token--> Python engine (FastAPI, 127.0.0.1) --> SQLite / files
        ^
        +-- Rust launcher starts the engine, makes port + token, stops it on exit
```

**Local API security**
- Binds to `127.0.0.1` only. Every endpoint requires header `X-BotFoundry-Token` (constant-time compare).
  Tokens shorter than 16 characters are refused at startup (`BOTFOUNDRY_TOKEN` env var).
- CORS allowlist: `http://localhost:1420`, `http://127.0.0.1:1420`, `http(s)://tauri.localhost`, `tauri://localhost`.
- Tauri generates a random free port and a random 256-bit token on every launch and hands them to React through
  one Tauri command, `get_engine_info`. In a plain browser (`npm run dev`) the app falls back to
  `.env.development.local` (`VITE_ENGINE_URL`, `VITE_ENGINE_TOKEN`; git-ignored via `*.local`).

**Engine lifecycle**
- Dev (`tauri dev`, debug build): Rust runs `engine\.venv\Scripts\python.exe -m botfoundry`, output to the terminal.
- Release: Rust runs the frozen `botfoundry-engine.exe` sitting next to the app exe; output goes to
  `%LOCALAPPDATA%\com.botfoundry.app\engine.log` (overwritten each launch).
- Engine gets `--parent-pid <app pid>`. A watchdog thread (`lifecycle.py`) checks every 2 s and shuts the engine
  down if the app is gone (crash, Ctrl+C, force-kill). On normal exit Rust also kills the whole process tree
  with `taskkill /PID <pid> /T /F` (a venv `python.exe` and a PyInstaller onefile exe are launcher + child).
  On Unix (secondary) the engine runs in its own process group and is killed with `kill -KILL -- -<pgid>`.

**Packaging**
- PyInstaller **onefile** frozen engine (about 14 MB, ~1 s startup on Linux; not yet measured on Windows),
  staged as a Tauri `externalBin` sidecar. `tauri.release.conf.json` adds the sidecar for release builds only,
  because `tauri dev` fails if a configured sidecar file is missing.
- Chosen over onedir for reliable Tauri sidecar handling. Revisit in Phase 6 if startup time or antivirus
  false positives are a problem (Defender may scan a freshly built unsigned exe; rerun if the smoke test times out).

**Workflow core (engine 0.4.0)** — details in `docs/node-contract.md`
- `NodeSpec`: id + version, typed ports (`any/string/number/boolean/object/list`), Pydantic config model,
  permissions, timeout, cancellable, async handler `(NodeContext, config, inputs) -> NodeResult`.
  Expected failures raise `NodeError(code, message, details)`.
- **Branching:** a node returns outputs only for active ports; missing ports are inactive; nodes fed only by
  inactive ports are skipped. `core.condition` returns `true` or `false`, never both.
- Workflow JSON: `schema_version, id, name, nodes[{id,type,version,config,title?,position?}], edges[{source,source_port,target,target_port}]`.
  Unknown fields are rejected. Exactly one trigger node. Required inputs need exactly one inbound edge.
- Validation never raises for bad workflows; it returns issues with stable codes (`schema.invalid`,
  `node.duplicate_id`, `node.unknown_type`, `node.invalid_config`, `edge.unknown_node`, `edge.unknown_port`,
  `edge.type_mismatch`, `edge.multiple_inputs`, `node.missing_input`, `graph.no_trigger`,
  `graph.multiple_triggers`, `graph.cycle`, `node.unreachable` (warning)) plus `required_permissions`.
- Built-in nodes so far: `core.manual_trigger`, `core.condition`, `core.transform`.
  Spec's recommended library (build later, not before the contract is stable): Manual Trigger, Scan Folder,
  Filter Items, File Hash, Transform Data, Generate Report, Approval Gate, Move Files, Git Inspector, Run Tests.

**Runner and events (implemented in engine 0.5.0; see `docs/node-contract.md` for the full tables)**
`Run(definition, registry)` then `await run.execute()`; `run.cancel()` is safe from any thread; `run.events.since(id)`
for polling; `run.snapshot()` for state. Statuses: run `created/validating/running/completed/failed/cancelled`,
node `pending/running/completed/failed/skipped/cancelled`. Timeouts really interrupt handlers; non-cancellable nodes
finish before a cancel takes effect; handler results and input types are checked at runtime; exactly one terminal
event per run. The `node_skipped` event was added beyond the spec's list.

**Persistence (engine 0.6.0; see `docs/persistence.md`)**
SQLite at `<data-dir>/botfoundry.db` (WAL). One connection + lock, explicit transactions. Append-only migrations applied
atomically (a database from a newer version is refused, never modified). Tables: `workflows`, `runs` (with the exact
`workflow_snapshot` that ran and a reserved `rulebook_version`), `node_runs`, `run_events`, `schema_migrations`.
`storage.attach(run)` before `execute()` mirrors every event and its state; database failures never break a run (logged,
counted in `persister.errors`). At start-up `mark_interrupted_runs()` fails any unfinished run with `run.interrupted`
and never resumes it. Data dir: `--data-dir`; dev `engine/.data` (git-ignored); packaged `%LOCALAPPDATA%\com.botfoundry.app`;
hand-started engine default `%LOCALAPPDATA%\BotFoundry`. `/health` reports `database_schema_version`. An unreadable database
stops start-up with exit code 2 and is left untouched.

**Event model (from the spec)**
Run created; validation started/succeeded/failed; node started; node progress; node completed; node failed;
approval requested; approval granted/denied; run cancelled; run completed; run failed. Events carry a stable id,
timestamp, run id, node id where relevant, and a structured payload. The engine keeps authoritative state for:
workflow definition, run state, node execution state, approval state, persisted results (the UI is never the record).
Transport: polling with a cursor first; SSE/WebSocket only if justified.

**Versioning**
- Engine version has one source: `engine/botfoundry/__init__.py`. App version must match in
  `package.json`, `src-tauri/Cargo.toml`, `src-tauri/tauri.conf.json`.
- Policy: `0.MINOR.PATCH` until 1.0 (MINOR = new capability/milestone, PATCH = fix only).

---

## 7. Repository layout

```
botfoundry/
  README.md
  .gitignore .gitattributes       (ignore venv, node_modules, target, dist, build output, binaries, databases, logs, secrets)
  docs/node-contract.md           node + workflow contract
  docs/PROJECT_HANDOFF.md         this file
  docs/persistence.md  docs/GIT_WORKFLOW.md
  scripts/bump.py                 versions + changelog sync/check
  scripts/build_engine.py         freeze engine, smoke-test, stage as sidecar
  shared/workflow-schemas/        reserved for exported JSON Schemas / generated TS types
  engine/
    pyproject.toml                dynamic version; extras: dev (pytest, httpx), build (pyinstaller)
    CHANGELOG.md
    run_engine.py                 entry point for the frozen exe
    botfoundry/
      __init__.py                 __version__  (single source of truth)
      __main__.py                 CLI: --port, --data-dir, --parent-pid; token from BOTFOUNDRY_TOKEN
      startup.py                  open_storage(): open DB, migrate, fail interrupted runs
      config.py                   Settings, TOKEN_HEADER, CORS origins
      lifecycle.py                is_process_alive, watch_parent
      api/app.py security.py routes_health.py
      workflow/ types.py node.py registry.py definition.py validation.py builtin_nodes.py events.py runner.py
      storage/ database.py migrations.py repository.py (Storage, RunPersister)
    tests/ test_health.py test_lifecycle.py test_version.py test_node_contract.py test_workflow_validation.py test_runner.py
             test_migrations.py test_storage.py test_startup.py
  apps/desktop/                   Tauri 2 + React + TypeScript
    CHANGELOG.md
    package.json  (scripts: dev, build, tauri, test = "vitest run")
    src/ App.tsx status.css api/engine.ts api/config.ts api/engine.test.ts   (main.tsx etc. are template files)
    src-tauri/ Cargo.toml (extra dependency: getrandom 0.2) tauri.conf.json tauri.release.conf.json
               src/ main.rs lib.rs engine.rs        binaries/ (git-ignored, built by build_engine.py)
```

Files the assistant wrote are in the snapshot zip (`BotFoundry_source_snapshot.zip`). Scaffold files generated by
`npm create tauri-app` (package.json, Cargo.toml, tauri.conf.json, main.tsx, icons, ...) are not in it; they live in my repo.

---

## 8. Command cheat sheet (PowerShell, from `D:\Projects\BotFoundry\botfoundry`)

```powershell
# First thing to do now: confirm Phase 1.1 and commit
.\engine\.venv\Scripts\python.exe -m pytest engine -q          # expect 64 passed
.\engine\.venv\Scripts\python.exe scripts\bump.py check        # expect: Version check OK (engine 0.4.0, app 0.4.0)
git add . ; git commit -m "Engine 0.4.0: node contract, registry, workflow validation"

# Engine tests / frontend tests
.\engine\.venv\Scripts\python.exe -m pytest engine -q
cd apps\desktop ; npm test

# Run the app in dev mode (Rust starts the engine itself; close the window with X, not Ctrl+C)
cd apps\desktop ; npm run tauri dev

# Run the engine by hand (testing only; fixed token)
$env:BOTFOUNDRY_TOKEN = "dev-token-1234567890abcdef"
.\engine\.venv\Scripts\python.exe -m botfoundry --port 8765
curl.exe -H "X-BotFoundry-Token: dev-token-1234567890abcdef" http://127.0.0.1:8765/health

# Versions and changelogs
.\engine\.venv\Scripts\python.exe scripts\bump.py engine 0.5.0   # or: app 0.5.0 ; adds a changelog stub
.\engine\.venv\Scripts\python.exe scripts\bump.py check          # fails on mismatch or leftover TODO

# Packaged build (no installer yet)
.\engine\.venv\Scripts\python.exe -m pip install -e "engine[dev,build]"
.\engine\.venv\Scripts\python.exe scripts\build_engine.py
cd apps\desktop
npm run tauri build -- --no-bundle --config src-tauri/tauri.release.conf.json
.\src-tauri\target\release\desktop.exe
Get-Content "$env:LOCALAPPDATA\com.botfoundry.app\engine.log"

# Git (see docs/GIT_WORKFLOW.md)
git status ; git switch -c <branch> ; git add <paths> ; git commit -m "feat(scope): summary"
git tag -a engine-v0.6.0 -m "..." ; git push origin --tags

# Look for leftover engine processes
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*botfoundry*' } | Select-Object ProcessId, CommandLine
Get-Process botfoundry-engine -ErrorAction SilentlyContinue
```

---

## 9. Problems already hit (so they are not repeated)

1. `python` resolved to MSYS2 Python 3.14.3 (no pip; `Activate.ps1` missing). Fix: `py -3.11 -m venv .venv` and call `.\.venv\Scripts\python.exe`.
2. `deactivate` only exists inside an activated venv; the "not recognized" error elsewhere is harmless.
3. `$env:BOTFOUNDRY_TOKEN = ...` only affects that one terminal and prints nothing; a second terminal does not have it.
4. `Ctrl+C` in `npm run tauri dev` skips the app's cleanup and used to orphan the engine. Fixed by the parent watchdog.
5. `tauri dev` fails if `externalBin` points at a missing file, hence the separate `tauri.release.conf.json`.
6. A packaged GUI app has no console, so child output must go to a file (done: `engine.log`).
7. PyInstaller onefile and venv launchers create two processes; kill the whole tree.
8. Python 3.14 is new; some wheels/PyInstaller may lag. The project uses 3.11 (requires >= 3.11).
9. Windows Defender may slow the first run of a newly built unsigned exe.

---

## 10. Known limitations / TODO

- No API endpoints or CLI to start, watch, cancel or list runs and workflows yet (Phase 1.4); persistence works but nothing in the app calls it yet.
- Approval events are defined but not emitted until Phase 2. Node outputs are not size-capped yet.
- Rulebook, permissions enforcement, The Fixer, canvas, Ledger: not started.
- Template `opener` Tauri plugin is still registered but unused.
- No installer or code signing yet (Phase 6). Frozen-engine Windows startup time not measured.
- Window title is still "desktop" and the UI has only the status screen.
- Schema export (Pydantic -> JSON Schema -> TypeScript) not built yet.
- Sequential execution only (loops and parallel branches are out of scope for the MVP).

---

## 11. Next milestone: Phase 1.4 (local API and CLI)

Goal: make everything built so far usable from outside the engine, so the UI (Phase 4) is only a client.
- **Endpoints** (all behind the existing token auth; Pydantic request/response models; stable error format):
  `GET /nodes` (catalog from `registry.catalog()`), `POST /workflows/validate`, `GET/PUT/DELETE /workflows(/{id})`,
  `POST /runs` (body: `workflow_id` or an inline definition), `GET /runs`, `GET /runs/{id}` (with node results),
  `GET /runs/{id}/events?after=<id>&limit=` (cursor polling), `POST /runs/{id}/cancel`.
- **Run manager:** starts `Run(...)` + `storage.attach(run)` + `execute()` as a background asyncio task, keeps active runs
  in a dict (for cancel), removes them when finished, and on engine shutdown cancels active runs and waits briefly so
  they end in a persisted terminal state. Reject starting a run for a workflow that fails validation (return the issues).
- **CLI** without breaking the existing invocation (`python -m botfoundry --port N --data-dir D --parent-pid P` must
  still start the server, because the Rust launcher and the frozen exe use it): add subcommands `serve` (default),
  `validate FILE`, `run FILE` (prints events as they happen, exit code 0/1/130 for completed/failed/cancelled), `nodes`.
- Persist-or-fail rule: if the database cannot record a run, tell the caller (include `persistence_errors` in the run
  response) rather than pretending history was saved.
- Tests with FastAPI `TestClient`: auth required on every endpoint, validate good/bad, CRUD, start/poll/cancel a run, event
  cursor, 404s, invalid bodies (422 in the project's error format), CLI via subprocess. Keep the frozen-exe smoke test green.
- Bump engine to 0.7.0, changelog entry, `bump.py check`; update `docs/` and this file; provide the git commit plan.

Then Phase 2 (Rulebook and enforcement) starts the safety layer: see section 5.

---

## 12. Prompt to resume in a new chat

Paste this, and attach: `Plan.docx`, this file, and `BotFoundry_source_snapshot.zip`.

> I'm building BotFoundry, a local-first Windows desktop automation platform (Tauri + React/TS + Python/FastAPI
> + SQLite). Plan.docx is the source of truth; PROJECT_HANDOFF.md records the current state, decisions,
> environment, commands and working agreement. The source snapshot has the code written so far. Read both
> documents first. Phase 0 is complete; Phase 1.1 (node contract, registry, validation), 1.2 (runner, events, timeouts,
> cancellation) and 1.3 (SQLite persistence, restart safety) are delivered, and I will confirm they pass on my
> machine. The repo is going on GitHub: give me a git commit plan with every step and keep .gitignore current.
> Continue with Phase 1.4 (local API and CLI) following section 11 of the handoff.
> Work in small tested steps, give exact PowerShell commands, update the changelog and bump the version, and
> tell me honestly what you could and could not verify. I know Python and React but not Rust.
