# Node contract and workflow format

Status: engine 0.10.0 (Phase 1.1 to 3.1). Code: `engine/botfoundry/workflow/`.

## A node (a registered, typed operation)
Every node type is a `NodeSpec` that defines:

| Field | Meaning |
|---|---|
| `id` + `version` | Stable identifier (`core.condition`) and an integer version. Workflows pin the version. |
| `config_model` | Pydantic model = the configuration schema (also exported as JSON Schema for the UI). |
| `inputs` / `outputs` | Typed ports (`any, string, number, boolean, object, list`). Inputs can be required. |
| `permissions` | Permission names the node needs (e.g. `fs.read`). Collected at validation for the Rulebook. |
| `handler` | `async (NodeContext, config, inputs) -> NodeResult`. |
| `timeout_seconds` | Enforced by the runner (Phase 1.2). |
| `cancellable` | Handlers receive a `CancelToken` and should check it. |
| result | `NodeResult(outputs, summary)` on success; `NodeError(code, message, details)` for expected failures. |

### Branching rule
A node returns outputs only for the ports that are **active**. Ports missing from `outputs` are
inactive, and nodes fed only by inactive ports are skipped. `core.condition` returns either
`true` or `false`, never both.

## A workflow (JSON)
`WorkflowDefinition`: `schema_version`, `id`, `name`, `nodes[]`, `edges[]`.
- node: `id`, `type`, `version`, `config`, optional `title`/`position` (layout only).
- edge: `source`, `source_port`, `target`, `target_port`.
- Unknown fields are rejected, so typos are caught instead of ignored.

## Validation (`validate_workflow`, `parse_and_validate`)
Never raises for bad workflows. Returns issues with stable codes, severity, and the node, edge or field.

| Code | Severity | Meaning |
|---|---|---|
| `schema.invalid` | error | Document structure is wrong |
| `node.duplicate_id` | error | Node id used twice |
| `node.unknown_type` | error | Type or version not registered |
| `node.invalid_config` | error | Config fails the node's schema (with field path) |
| `edge.unknown_node` / `edge.unknown_port` | error | Edge points at something that does not exist |
| `edge.type_mismatch` | error | Port types incompatible |
| `edge.multiple_inputs` | error | Input has more than one incoming edge |
| `node.missing_input` | error | Required input not connected |
| `graph.no_trigger` / `graph.multiple_triggers` | error | Exactly one trigger required |
| `graph.cycle` | error | Cycle (including self-loops) |
| `node.unreachable` | warning | Not reachable from the trigger |

The result also lists `required_permissions`: the union of all nodes' permissions.

## Execution (`workflow/runner.py`)
```python
run = Run(definition, registry)      # emits run_created
unsubscribe = run.events.subscribe(callback)   # optional live listener
await run.execute()                  # validate, then run nodes in order
run.cancel()                         # any time, any thread
run.snapshot()                       # JSON-friendly state of the run and every node
```
- Run status: `created, validating, running, waiting_approval, completed, failed, cancelled`.
- Node status: `pending, running, waiting_approval, completed, failed, skipped, cancelled`.
- **Failure policy:** the first failed node stops the run; remaining nodes are `skipped` with a reason.
- **Timeouts:** `NodeSpec.timeout_seconds` per node. On timeout the handler task is cancelled and the node fails with `node.timeout`.
- **Cancellation:** cancellable nodes are interrupted immediately; non-cancellable nodes finish, then the run stops as `cancelled`.
- **Runtime checks** (a node can never return something the contract does not allow): input types
  (`node.invalid_input`), result shape and output ports/types (`node.invalid_result`).
- Error codes written to `node.error.code`: `node.timeout`, `node.unexpected_error`, `node.invalid_input`,
  `node.invalid_result`, `permission.denied`, `permission.undeclared`, `node.approval_denied`,
  `limit.max_files_per_run`, `run.time_limit`, path codes (`path.*`), or any code a handler raises with `NodeError`.

## Events
Each event: `id` (1, 2, 3... per run), `run_id`, `type`, `timestamp` (UTC), `node_id` (when relevant), `payload`.

| Type | Payload |
|---|---|
| `run_created` | workflow_id, workflow_name, node_count, rulebook (`{bot_id, version}` or null) |
| `validation_started` | - |
| `validation_succeeded` | warnings[], required_permissions[] |
| `validation_failed` | issues[] |
| `node_started` | type, version |
| `node_progress` | fraction (0..1 or null), message |
| `node_completed` | summary, output_ports[], duration_ms |
| `node_failed` | error{code,message,details}, duration_ms |
| `node_skipped` | reason |
| `run_completed` | completed, skipped, duration_ms |
| `run_failed` | error, failed_node, duration_ms |
| `run_cancelled` | reason, running_node, not_started[], duration_ms |
| `approval_requested` | approval_id, kind, summary, details, timeout_seconds, expires_at |
| `approval_granted` | approval_id, decided_by, reason (`approved`), note |
| `approval_denied` | approval_id, decided_by, reason (`denied`, `timeout`, `cancelled`, `run_failed`, `interrupted`), note |

Exactly one of `run_completed`, `run_failed`, `run_cancelled` ends every run. Poll with `events.since(last_id)`.

## Built-in nodes
| Id | Category | Ports | Config |
|---|---|---|---|
| `core.manual_trigger` | trigger | out `payload` (object) | `payload` |
| `core.condition` | logic | in `value`; out `true`, `false` (only one is active) | `operator`, `compare_to` |
| `core.transform` | data | in `value`; out `result` | `operation`: to_upper, to_lower, length, to_string |
| `core.wait` | logic | in `value`; out `result` (same value) | `seconds` 0 to 60 (default 1) |
| `core.approval_gate` | approval | in `value`; out `result` (same value) | `summary`, `details`. Pauses until a person approves |
| `files.scan_folder` | files | optional `start`; out `items`, `skipped`, `stats` | `folder`, `recursive`, `max_depth`, `include_hidden`, `max_items`. Needs `fs.read` |
| `files.filter` | data | in `items`; out `matched`, `rejected`, `stats` | extensions, names, sizes, ages |
| `files.hash` | files | in `items`; out `items`, `failed`, `stats` | `algorithm`, `skip_unique_sizes`. Needs `fs.read` |
| `files.find_duplicates` | data | in `items`; out `groups`, `duplicates`, `stats` | `keep`, `ignore_empty` |

The file nodes are described in `docs/files.md`.

## What a node can use while it runs (`NodeContext`)
| Hook | Purpose |
|---|---|
| `ctx.report_progress(fraction, message)` | Progress events |
| `ctx.cancel` | Cooperative cancellation token |
| `ctx.authorize(permission, paths)` | Ask the engine; returns the resolved paths to act on, or raises `NodeError` (see `docs/rulebook.md`) |
| `await ctx.request_approval(summary, details)` | Pause until a person approves; raises if denied or unanswered |
| `ctx.budget.consume_files(n)` | Count files against the Rulebook's per-run limit |

A node that declares `permissions` is checked **before** it runs; see `docs/rulebook.md` for the full rules.
Outside a run (for example a handler called directly in a test) the defaults deny everything.
