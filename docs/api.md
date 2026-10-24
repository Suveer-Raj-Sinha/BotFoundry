# Local API and CLI

Status: engine 0.9.0 (Phase 1.4, Rulebook endpoints from Phase 2.1, approval endpoints from Phase 2.2). Code: `engine/botfoundry/api/`, `run_manager.py`, `cli.py`.

The engine listens on `127.0.0.1` only. **Every endpoint requires the header `X-BotFoundry-Token`**
(the desktop app gets a random one each launch). All bodies are JSON.

## Endpoints
| Method and path | Purpose | Success |
|---|---|---|
| `GET /health` | Engine, Python and database versions | 200 |
| `GET /nodes` | Catalog of node types: config schema, ports, permissions | 200 |
| `POST /workflows/validate` | Check a workflow document; problems are in the result | 200 |
| `GET /workflows` | Saved workflows (summaries) | 200 |
| `GET /workflows/{id}` | One saved workflow | 200 / 404 |
| `PUT /workflows/{id}` | Create or replace. Saved even if it does not validate yet; the validation result is returned | 200 / 422 |
| `DELETE /workflows/{id}` | Delete the saved workflow (past runs keep their own copy) | 200 / 404 |
| `POST /runs` | Start a run: `{"workflow_id": "..."}` **or** `{"definition": {...}}` | 202 / 404 / 422 / 429 |
| `GET /runs?limit=&offset=&workflow_id=` | Run history, newest first | 200 |
| `GET /runs/{id}` | Run with every node's status, outputs and error; `active`; `persistence_errors` | 200 / 404 |
| `GET /runs/{id}/events?after=&limit=` | Events after a cursor (see polling below) | 200 / 404 |
| `POST /runs/{id}/cancel` | Ask an active run to cancel | 202 / 404 / 409 |
| `GET /approvals` | Everything waiting for a person right now, across active runs | 200 |
| `GET /runs/{id}/approvals` | All of a run's approval requests and their outcomes | 200 / 404 |
| `POST /runs/{id}/approvals/{approval_id}` | Approve or deny: `{"approve": true, "note": "..."}` | 200 / 404 / 409 |
| `GET /permissions` | The permission vocabulary with risk and the system limit for each | 200 |
| `POST /rulebooks/validate` | Check a Rulebook document; problems are in the result | 200 |
| `GET /rulebooks`, `GET /rulebooks/{bot}` | Latest Rulebook per bot / one bot's latest | 200 / 404 |
| `GET /rulebooks/{bot}/versions[/{n}]` | Version history / one version | 200 / 404 |
| `PUT /rulebooks/{bot}?note=` | Save a new version (refused if it has any error; unchanged content makes no new version) | 200 / 422 |
| `POST /rulebooks/{bot}/evaluate` | Dry run: what the engine would decide for a permission and paths | 200 / 404 |

`POST /runs` also pins the bot's latest Rulebook for the run (`docs/rulebook.md`). It validates first. An invalid workflow returns **422 `workflow_invalid`** with the issues and no run
is created. A valid one returns 202 immediately; the run continues in the background.

## Errors
One format everywhere (status codes are the usual HTTP ones):
```json
{"error": {"code": "workflow_invalid", "message": "...", "details": {"issues": [...]}}}
```
Codes: `unauthorized`, `not_found`, `method_not_allowed`, `invalid_request` (details list the problem fields, never
the submitted values), `workflow_not_found`, `workflow_invalid`, `workflow_invalid_structure`, `id_mismatch`,
`run_not_found`, `run_not_active`, `approval_not_found`, `approval_already_decided`, `rulebook_required` (nodes need permissions but the workflow has no bot or Rulebook), `rulebook_not_found`, `rulebook_invalid`, `too_many_active_runs` (limit 8), `storage_error`, `storage_unavailable`,
`internal_error` (details are only in `engine.log`).

Run status values: `created`, `validating`, `running`, `waiting_approval`, `completed`, `failed`, `cancelled`.
Node status values add `pending`, `skipped` and `waiting_approval`.

## Following a run (polling)
```
after = 0
loop:
    page = GET /runs/{id}/events?after={after}
    show page.events
    after = page.next_after
    if page.finished and not page.has_more: stop
    otherwise wait ~250 ms and repeat
```
The status is read before the events, so `finished: true` means every event up to the end was already stored:
the last events cannot be missed. Event ids are 1, 2, 3... per run, so a client can detect gaps.
Event types and payloads: see `docs/node-contract.md`.

`persistence_errors` on a run is normally empty. If it is not, the run executed but part of its history could
not be saved; tell the user.

## Examples (PowerShell)
```powershell
$env:BOTFOUNDRY_TOKEN = "dev-token-1234567890abcdef"
# terminal 1: start the engine
.\engine\.venv\Scripts\python.exe -m botfoundry --port 8765 --data-dir .\engine\.data

# terminal 2:
$h = @{ "X-BotFoundry-Token" = "dev-token-1234567890abcdef" }
Invoke-RestMethod http://127.0.0.1:8765/nodes -Headers $h
$wf = Get-Content examples\hello.workflow.json -Raw
$run = Invoke-RestMethod http://127.0.0.1:8765/runs -Method Post -Headers $h -ContentType "application/json" -Body ('{"definition": ' + $wf + '}')
Start-Sleep 1
(Invoke-RestMethod "http://127.0.0.1:8765/runs/$($run.run_id)/events" -Headers $h).events | Select-Object id, type, node_id
```

## Shutdown behavior
When the engine stops (window closed, parent gone, Ctrl+C) it cancels active runs and waits briefly, so each
ends in a recorded `cancelled` state. Anything that still escapes (power loss, hard kill) is marked
`run.interrupted` at the next start (see `docs/persistence.md`).

## Command line
```
python -m botfoundry [--port N] [--data-dir D] [--parent-pid P]   start the engine (the desktop app uses this)
python -m botfoundry nodes [--json]                                 list node types
python -m botfoundry validate FILE [--json]                         check a workflow file
python -m botfoundry run FILE [--data-dir D] [--no-save] [--json] [--rulebook R] [--approve ask|yes|no]
                                                                    run a workflow, printing events live; --approve
                                                                    answers approval requests (default: ask on the terminal)
```
Rulebook commands are in `docs/rulebook.md`.

Exit codes: `0` ok / completed / allowed, `3` needs approval (`rulebook check` only), `1` invalid or failed, `2` usage or environment problem (missing file or token,
unreadable database), `130` cancelled (Ctrl+C). `run` saves to the database unless `--no-save`.
Try the files in `examples/` (`hello`, `branching`, `failing`, `slow`). The packaged `botfoundry-engine.exe`
accepts the same commands.

**One engine per data folder.** Only the engine process (`serve`) does crash recovery at start-up. `run` never
does, so it cannot mislabel another process's live runs; but do not start two engines on the same folder.
