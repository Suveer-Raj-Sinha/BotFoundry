# BotFoundry Desktop App Changelog

Tauri + React + TypeScript desktop app. The version must match in three files:
`package.json`, `src-tauri/Cargo.toml`, `src-tauri/tauri.conf.json`.
Change it with `python scripts/bump.py app X.Y.Z` from the repo root.
Format: [Keep a Changelog](https://keepachangelog.com). Versioning: 0.MINOR.PATCH until 1.0.

## [0.4.0] - 2026-10-16

### Added
- Release-mode launcher: a packaged build runs the frozen `botfoundry-engine` sidecar from next to
  the app executable, with the same random port, random token and parent-PID watchdog as dev mode.
- Engine output in release builds goes to `engine.log` in `%LOCALAPPDATA%\com.botfoundry.app`
  (overwritten on each launch).
- `src-tauri/tauri.release.conf.json`: adds the sidecar to release builds only, so `tauri dev`
  does not require a built engine.
- Clear error on screen if the engine executable is missing.

### Changed
- Dev and release modes now share one startup path in `engine.rs`.
- On Unix the engine runs in its own process group and the whole group is killed on exit
  (secondary platform; Windows uses `taskkill /T`).

## [0.3.0] - 2026-10-16

### Added
- Rust engine launcher (`src-tauri/src/engine.rs`): picks a free loopback port, creates a random
  256-bit token, starts the engine from the dev virtual environment, hides the console window,
  and stops the whole process tree (`taskkill /T`) when the app exits.
- Passes `--parent-pid` so the engine also stops itself if the app dies abruptly.
- `get_engine_info` Tauri command. React now gets the engine address and token from Rust
  (the `.env.development.local` fallback only applies in a plain browser).
- Startup behavior: shows "Starting engine..." with 1-second retries until connected,
  then checks every 5 seconds.

### Changed
- Removed the template's `greet` demo command.

### Known limitations
- Dev mode only (fixed in 0.4.0).
- The template's `opener` plugin is still registered but unused.

## [0.2.0] - 2026-10-16

### Added
- Engine status screen (version, Python version, local AI availability) with live polling.
- Typed health client (`src/api/engine.ts`) with separate errors for unreachable,
  unauthorized, and malformed responses, plus a 3-second timeout. It never invents a result.
- 6 Vitest tests for the health client.
- Dark theme styling and accessible focus/live-region behavior.

## [0.1.0] - 2026-10-16

### Added
- Tauri 2 + React + TypeScript + Vite scaffold (identifier `com.botfoundry.app`).
