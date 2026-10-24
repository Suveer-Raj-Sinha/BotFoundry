# Persistence (SQLite)

Status: engine 0.9.0 (Phase 1.3, migration 2 from Phase 2.1, approvals from Phase 2.2). Code: `engine/botfoundry/storage/`.

## Where data lives
`<data-dir>/botfoundry.db` (WAL mode, so you will also see `-wal` and `-shm` files while it is open).
- Desktop app, development: `engine/.data/` (git-ignored).
- Desktop app, packaged: `%LOCALAPPDATA%\com.botfoundry.app\`, next to `engine.log`.
- Engine started by hand without `--data-dir`: `%LOCALAPPDATA%\BotFoundry\` (Linux/macOS: `~/.local/share/botfoundry/`).

## Schema (migrations 1 and 2)
| Table | Contents |
|---|---|
| `workflows` | id, name, definition (JSON), created_at, updated_at |
| `runs` | run_id, workflow_id, workflow_name, **workflow_snapshot** (the exact definition that ran), status, error, `rulebook_version` (reserved for Phase 2), created_at, finished_at |
| `node_runs` | per node of a run: status, outputs, summary, error, reason, started_at, finished_at |
| `run_events` | the full ordered event log: (run_id, id) primary key, type, timestamp, node_id, payload |
| `rulebook_versions` | (bot_id, version) primary key, name, the Rulebook document, note, created_at. Immutable: versions are only ever added |
| `schema_migrations` | which migrations have been applied |

`runs.rulebook_bot_id` and `runs.rulebook_version` record the Rulebook a run followed (empty when it had none).
Approvals have no table of their own: they are derived from the `approval_*` events, the one authoritative record
(`GET /runs/{id}/approvals`).

Deleting or editing a workflow never changes past runs, because each run keeps its own snapshot.

## Migrations
Ordered and append-only in `storage/migrations.py`. Never edit one that has shipped; add a new one.
Each migration is applied in a single transaction with its bookkeeping row, so a failure leaves the
database unchanged. A database written by a newer BotFoundry is refused with a clear message, not modified.

## How runs are saved
`storage.attach(run)` before `await run.execute()`. Every event is written in a transaction together with
the state it implies (node row, run status). A database failure never breaks the run: it is logged and
recorded in `persister.errors` so the UI can warn that history may be incomplete.

## Restart safety
At start-up `mark_interrupted_runs()` finds runs still marked created, validating, running or waiting_approval. They are
marked **failed** with code `run.interrupted` (nodes: `node.interrupted`, or skipped if never started) and a
unanswered approval requests are closed with `approval_denied` (reason `interrupted`) and a final `run_failed` event is appended. Runs are never resumed, so work is never executed twice.

## Reading
`list_workflows`, `get_workflow`, `save_workflow`, `delete_workflow`, `list_runs(limit, offset, workflow_id)`,
`get_run(run_id)`, `get_run_workflow(run_id)`, `get_events(run_id, after_id, limit)`.

## Limits worth knowing
- Node outputs are stored as JSON; unusual values are stored as text (`default=str`).
- Large outputs (for example file lists from the Fixer) are not size-capped yet.
- One connection with a lock: right for a single-user local app, not for a server.

## Several processes
Only the engine (`serve`) performs crash recovery at start-up. `botfoundry run` opens the same database
without recovery, so it can never mark another process's active runs as interrupted. Do not start two
engines on the same data folder (no lock yet).
