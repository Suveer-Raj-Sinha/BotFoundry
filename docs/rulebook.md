# Rulebooks and path authorization

Status: engine 0.8.0 (Phase 2.1: the rules, their validation, versions, API and CLI).
**Not yet enforced during runs.** Making the runner obey Rulebooks and pause at approval gates is Phase 2.2.
Code: `engine/botfoundry/rules/`.

## What a Rulebook is
One JSON document per bot saying what that bot may do. The engine, never the UI, applies it. Every
change is saved as a new immutable version, and (from Phase 2.2) each run records which version it used.

```json
{
  "schema_version": 1,
  "bot_id": "downloads-cleaner",
  "name": "Downloads cleaner",
  "allowed_folders":   ["C:\\Users\\Me\\Downloads"],
  "forbidden_folders": ["C:\\Users\\Me\\Downloads\\Keep"],
  "default_policy": "deny",
  "permissions": {"fs.read": "allow", "fs.move": "ask", "fs.delete": "deny"},
  "limits":   {"max_files_per_run": 500, "max_run_seconds": 600},
  "approval": {"timeout_seconds": 300}
}
```
| Field | Meaning |
|---|---|
| `allowed_folders` | The only places the bot may touch. Absolute paths, a specific folder (never a drive root). |
| `forbidden_folders` | Carve-outs inside the allowed folders that are always off limits. |
| `permissions` | Per permission: `allow`, `ask` (needs your approval each time) or `deny`. |
| `default_policy` | For permissions not listed. Keep it `deny`. |
| `limits` | Caps per run (enforced from Phase 2.2). |
| `approval.timeout_seconds` | An approval nobody answers in this time counts as **denied**. |

Workflows name their governing bot with the optional `bot_id` field. A workflow with no bot, or a bot with no
Rulebook, gets **no** sensitive permissions.

## Order of authority
1. **System safety constraints** (cannot be overridden): `fs.delete` and `network.access` are not available to
   bots; `process.run_tests` can only ever be `ask`; protected locations (Windows folder, Program Files,
   ProgramData, AppData, and BotFoundry's own data folder) can never be touched; an approved folder cannot be a
   drive root, a network share root or a whole user profile.
2. **The bot's Rulebook.**

A decision is never more permissive than either. A Rulebook that asks for more than the system allows is
**refused at save time** (`rulebook.exceeds_system_limit`) and, as defense in depth, still capped at run time.

## Permissions
| Permission | What it covers | Risk | System limit |
|---|---|---|---|
| `fs.read` | Read files and listings in approved folders | low | allow |
| `fs.write` | Create or change files | medium | allow |
| `fs.move` | Move or rename files | medium | allow |
| `fs.delete` | Delete files | high | **deny** |
| `git.read` | Inspect git repositories | low | allow |
| `process.run_tests` | Run a project's tests | high | **ask** |
| `network.access` | Use the network | high | **deny** |

`GET /permissions` returns this table. Nodes declare the permissions they need; validation collects them.

## Validation (a Rulebook with any error cannot be saved)
Errors: `schema.invalid`, `rulebook.folder_invalid`, `rulebook.folder_too_broad`, `rulebook.folder_protected`,
`rulebook.folder_conflict` (approved folder entirely inside a forbidden one), `rulebook.unknown_permission`
(catches typos like `fs.mvoe`), `rulebook.exceeds_system_limit`.
Warnings (saved, but shown): `rulebook.folder_duplicate`, `rulebook.folder_nested`, `rulebook.forbidden_outside`,
`rulebook.permissive_default`, `rulebook.no_folders`, `rulebook.no_file_limit`.
Unlike workflows (which may be saved half-finished), a Rulebook is a safety control, so an invalid one never
becomes the active one.

## Versions
`PUT /rulebooks/{bot_id}` creates version N+1 only if the content changed (key order and defaults are
ignored). Versions are never edited or deleted; going back to old content is a new version.

## Path authorization (`rules/paths.py`)
A path is judged lexically and then physically.

**Lexical (identical on every host, tested on Linux and Windows):**
- Case-insensitive like NTFS; `/` and `\` both accepted; `.` and `..` collapsed (`..` stops at the root);
  trailing dots and spaces ignored (as Windows does, so `Downloads. ` cannot sneak past); `\\?\` and
  `\\?\UNC\` prefixes understood.
- Rejected: relative and drive-relative paths (`C:folder`), reserved device names (`CON`, `NUL.txt`, `COM1`,
  `LPT9`, `COM¹`...), alternate data streams (`file.txt:evil`), wildcards, control characters, device paths
  (`\\.\`, volume GUIDs, GLOBALROOT) and Windows 8.3 short names (`PROGRA~1`), which can alias protected folders.
- Containment compares whole folder names, so `C:\Allowed2` is **not** inside `C:\Allowed`.

**Physical (on the real machine):** symlinks and junctions are followed and the real location must still be
inside the real approved folder, outside forbidden folders and outside protected ones. This also covers a
destination that does not exist yet (the existing part is resolved, the rest appended). On POSIX hosts `..`
after a link is judged by where the kernel really goes.

Always act on the returned `resolved` path, not on the text you were given.

**Limits:** the check is a snapshot; a file can change before use, so destructive steps must re-check
immediately before acting. Mounted volumes that resolve to `\\?\Volume{...}` paths are refused. The
Windows-only tests for real junctions and symlinks (`tests/test_paths.py`, marked `windows_only`) are skipped
on other systems; run `pytest` on Windows to exercise them.

## API
`GET /permissions`, `POST /rulebooks/validate`, `GET /rulebooks`, `GET /rulebooks/{bot}`,
`GET /rulebooks/{bot}/versions`, `GET /rulebooks/{bot}/versions/{n}`, `PUT /rulebooks/{bot}?note=`,
`POST /rulebooks/{bot}/evaluate` (dry run: `{"permission": "fs.move", "paths": [...]}` returns allow, ask or
deny with the reason and a verdict per path). See `docs/api.md`.

## CLI
```
python -m botfoundry rulebook validate FILE [--json]
python -m botfoundry rulebook check FILE PERMISSION [PATH ...]    exit 0 allow, 3 ask, 1 deny
```
`examples/downloads-cleaner.rulebook.json` is a ready example (replace `YourName`).

## Next (Phase 2.2)
The runner will evaluate each node's declared permissions against the bot's Rulebook before running it, nodes
will request authorization for specific paths while they run, `ask` outcomes will pause the run at an approval
gate (`approval_requested` / `granted` / `denied` events, answer through the API, timeout = denied), and each
run will record the Rulebook version it followed.
