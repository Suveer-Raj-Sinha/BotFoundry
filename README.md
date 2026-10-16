# BotFoundry

Your tools. Your bots. Your machine. Real work.

Local-first Windows desktop automation platform (Tauri + React/TS + Python/FastAPI + SQLite).

## Status
Phase 0a: Python engine skeleton (health endpoint, per-launch token auth, CORS allowlist, tests).

## Run the engine (Windows PowerShell)
    cd engine
    python -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -e ".[dev]"
    pytest
    $env:BOTFOUNDRY_TOKEN = "change-me-to-a-long-random-string"
    python -m botfoundry --port 8765

Then: Invoke-RestMethod http://127.0.0.1:8765/health -Headers @{ "X-BotFoundry-Token" = $env:BOTFOUNDRY_TOKEN }
