# Rulebooks and path authorization

Status: engine 0.9.0 (Phase 2.1 the rules, validation, versions, API and CLI; Phase 2.2 enforcement during runs
and approval gates). Code: `engine/botfoundry/rules/`, enforcement in `workflow/runner.py`.

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

## How enforcement works (runs)
1. **Start.** If any node in the workflow declares permissions, the workflow must have a `bot_id` and that bot
   must have a Rulebook. Otherwise **the run is refused** (API: 422 `rulebook_required`; CLI: validation fails;
   nothing is created or run). The bot's latest Rulebook version is **pinned for the whole run**: saving a newer
   version mid-run changes nothing for runs already started. It is re-validated first under today's system
   rules; a stored Rulebook that no longer passes is not used (422 `rulebook_invalid`).
2. **Before each node runs,** the engine checks every permission the node declares. `deny` (by the Rulebook, by
   default-deny, or by a system limit) fails the node (`permission.denied`) and **none of its code runs**.
   `ask` pauses the run at an **approval** (below) before the node starts.
3. **While a node runs,** it must act through the engine: `ctx.authorize(permission, paths)` checks the paths
   against the approved, forbidden and protected folders and returns the **resolved** paths to use; a refused
   path fails the node with the path's code (`path.outside_allowed`, `path.forbidden`...). A node can only
   authorize permissions it declared (`permission.undeclared`). `ctx.budget.consume_files(n)` enforces
   `max_files_per_run` across the whole run (`limit.max_files_per_run`).
4. **Limits.** `max_run_seconds` ends the run with `run.time_limit` when nodes work too long; time spent waiting
   for a person is not counted, and neither is it counted against a node's own timeout.
5. **Record.** Every run stores the Rulebook bot and version it followed (`run_created` event, `runs` table, API).

**For node authors:** never touch the filesystem with paths you were not given by `ctx.authorize`. A node is
only as safe as its use of these hooks, so file-changing nodes must be reviewed against this rule.

## Approvals
Two ways a run asks a person:
- **Engine approval** (`kind: node_permissions`): a permission whose policy is `ask`. One request per node
  execution, listing the permissions. Can't be bypassed by editing the workflow.
- **Explicit approval** (`kind: explicit`): a node calls `ctx.request_approval(summary, details)`; the built-in
  `core.approval_gate` node does exactly this and shows a preview of the value that is about to pass through.

While waiting, the run status is `waiting_approval` and the node shows `waiting_approval`. Answer with
`POST /runs/{id}/approvals/{approval_id}` (`{"approve": true, "note": "..."}`), see everything waiting with
`GET /approvals`, or on the CLI with `--approve ask|yes|no` (default: ask on the terminal; with no terminal,
deny). **No answer within `approval.timeout_seconds` counts as a denial** (`reason: timeout`).
A denial fails the node with `node.approval_denied`, and the run stops.

Each request's outcome has an engine `reason` (`approved`, `denied`, `timeout`, `cancelled`, `run_failed`,
`interrupted`) kept separate from the person's free-text `note`, so text can never impersonate an outcome.
If the run is cancelled, fails or is interrupted by a crash, open requests are closed as denied; a run never
ends with an unanswered question.
