# BotFoundry

**Your tools. Your bots. Your machine. Real work.**

BotFoundry is a local-first Windows desktop automation platform. You build bots as typed node
workflows, give each bot a Rulebook that controls what it may touch, and watch every action run on your
own machine. No cloud account, no telemetry; AI is optional and never required.

> **Status: early development.** The engine core is complete and tested: a secure local engine with an HTTP
> API and a CLI, a workflow core (validation, execution, events) and SQLite persistence, inside a desktop
> shell. The Rulebook, the first real bot (The Fixer, a file organizer) and the workflow editor are next.
> See the roadmap.

## What works today
- **Desktop app** (Tauri 2 + React + TypeScript) that starts its own engine and shows live engine status.
- **Local engine** (Python + FastAPI) bound to `127.0.0.1`, protected by a random per-launch token and an
  origin allowlist. It shuts itself down if the app dies.
- **Workflow core:** a node contract with typed ports, a registry, workflow validation with structured
  error codes, and a runner with branching, timeouts, cancellation and a structured event log.
- **Persistence:** SQLite with migrations; runs, node results and events survive restarts, and
  interrupted runs are failed, never resumed (no duplicate execution).
- **Local API and CLI:** validate, save and run workflows, follow a run's events, cancel it, browse history
  (`docs/api.md`); `python -m botfoundry run examples/hello.workflow.json` runs one from the terminal.
- **Packaging:** the engine is frozen into one executable and bundled as a Tauri sidecar.
- **Rulebooks, enforced:** versioned per-bot permissions and approved folders with conflict detection, system
  limits no Rulebook can exceed, Windows-aware path authorization, and a runner that checks every node's
  permissions before it runs. Actions that need approval pause the run until a person answers; no answer counts as
  a denial. Each run records the exact Rulebook version it followed (`docs/rulebook.md`).
- 372 engine tests, 6 frontend tests; every release has a changelog.

## Architecture
```
Tauri window (React UI) --HTTP + token--> Python engine (FastAPI, 127.0.0.1) --> SQLite / your files
        ^
        +-- Rust launcher: starts the engine with a random port + token, stops it on exit
```
The engine, not the UI, enforces permissions. Results are never invented: a node that fails, times out
or returns something its contract does not allow is reported as failed.

## Getting started (Windows)
Requirements: Python 3.11+, Node 20+, Rust (MSVC toolchain) and Git.
```powershell
git clone https://github.com/<your-username>/botfoundry.git
cd botfoundry\engine
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest                     # engine tests
cd ..\apps\desktop
npm install
npm test                                                 # frontend tests
npm run tauri dev                                        # run the desktop app (it starts the engine)
```
Packaged build (no installer yet): see `docs/GIT_WORKFLOW.md` for release steps and the commands below.
```powershell
cd botfoundry
.\engine\.venv\Scripts\python.exe -m pip install -e "engine[dev,build]"
.\engine\.venv\Scripts\python.exe scripts\build_engine.py
cd apps\desktop
npm run tauri build -- --no-bundle --config src-tauri/tauri.release.conf.json
```

## Repository layout
```
engine/      Python engine: API, workflow core (node contract, validation, runner, events), storage
apps/desktop Tauri + React desktop app
scripts/     bump.py (versions + changelogs), build_engine.py (freeze + smoke-test the engine)
examples/    ready-to-run workflow files
docs/        api.md, rulebook.md, node-contract.md, persistence.md, GIT_WORKFLOW.md, PROJECT_HANDOFF.md
```

## Roadmap
1. **Engine core** (done): node contract, validation, runner, events, persistence, local API and CLI.
2. **Rulebook** (done): per-bot permissions, approved folders, action policies, approval gates, enforced by
   the engine.
3. **The Fixer:** scan, hash, find duplicates, preview, approve, move safely, recover and undo.
4. **Interface:** workflow canvas, live run view, history.
5. Polish, scheduling, installer; then The Builder, optional local AI, templates.

## Try it
```powershell
cd engine
.\.venv\Scripts\python.exe -m botfoundry nodes
.\.venv\Scripts\python.exe -m botfoundry validate ..\examples\branching.workflow.json
.\.venv\Scripts\python.exe -m botfoundry run ..\examples\branching.workflow.json --no-save
```

## Try an approval
```powershell
.\.venv\Scripts\python.exe -m botfoundry run ..\examples\approval.workflow.json --no-save
```
The run pauses and asks on the terminal; `--approve yes` or `--approve no` answers automatically.

## Versions
The engine and the app are versioned separately (`engine/CHANGELOG.md`, `apps/desktop/CHANGELOG.md`).
`python scripts/bump.py check` verifies versions and changelogs agree.

## Contributing and history
Small, tested commits with conventional messages. See `docs/GIT_WORKFLOW.md`.

## License
To be decided before the first public release (see `docs/GIT_WORKFLOW.md`).
