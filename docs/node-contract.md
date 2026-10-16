# Node contract and workflow format

Status: engine 0.4.0 (Phase 1.1). Code: `engine/botfoundry/workflow/`.

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
