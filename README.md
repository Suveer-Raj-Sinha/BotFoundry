# BotFoundry

Your tools. Your bots. Your machine. Real work.

Local-first Windows desktop automation platform (Tauri + React/TS + Python/FastAPI + SQLite).

## Status
Phase 0b: desktop shell. Engine 0.4.0, app 0.4.0. See `engine/CHANGELOG.md` and `apps/desktop/CHANGELOG.md`.

## Versions and changelogs
- Engine version: `engine/botfoundry/__init__.py` (single source). App version: `package.json`,
  `src-tauri/Cargo.toml`, `src-tauri/tauri.conf.json` (must match).
- Every change that adds or alters behavior gets a changelog entry and a version bump:
      python scripts/bump.py engine 0.3.0
      python scripts/bump.py app 0.4.0
      python scripts/bump.py check
- 0.MINOR.PATCH until 1.0: MINOR = new capability or milestone, PATCH = fix only.

## Run the engine (Windows PowerShell)
    cd engine
    python -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -e ".[dev]"
    pytest
    $env:BOTFOUNDRY_TOKEN = "change-me-to-a-long-random-string"
    python -m botfoundry --port 8765

Then: Invoke-RestMethod http://127.0.0.1:8765/health -Headers @{ "X-BotFoundry-Token" = $env:BOTFOUNDRY_TOKEN }

## Build a packaged app (Windows, no installer yet)
    engine\.venv\Scripts\python.exe -m pip install -e "engine[dev,build]"
    engine\.venv\Scripts\python.exe scripts\build_engine.py
    cd apps\desktop
    npm run tauri build -- --no-bundle --config src-tauri/tauri.release.conf.json
    src-tauri\target\release\desktop.exe
Engine log in packaged builds: %LOCALAPPDATA%\com.botfoundry.app\engine.log
