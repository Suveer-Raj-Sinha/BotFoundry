"""Command line interface.

    botfoundry [--port N] [--data-dir D] [--parent-pid P]   start the engine (default; what the desktop app uses)
    botfoundry serve ...                                     same, explicit
    botfoundry nodes [--json]                                list the node types
    botfoundry validate FILE [--json]                        check a workflow file
    botfoundry run FILE [--data-dir D] [--no-save] [--json]  run a workflow file, printing events live

Exit codes:  0 ok / completed   1 invalid or failed   2 usage or environment problem   130 cancelled
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .config import DATABASE_FILE, Settings, default_data_dir
from .workflow.builtin_nodes import build_default_registry
from .workflow.events import EventType, RunEvent
from .workflow.runner import Run, RunStatus
from .workflow.validation import ValidationResult, parse_and_validate

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_CANCELLED = 0, 1, 2, 130


class CliError(Exception):
    """A problem to show the user (no traceback) and exit with EXIT_USAGE."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(f"File not found: {path}")
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(f"Cannot read {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise CliError(f"{path} is not valid JSON: {exc}")


def _print_issues(result: ValidationResult, stream: Any = None) -> None:
    stream = stream or sys.stdout
    for issue in result.issues:
        where = f" [{issue.node_id}]" if issue.node_id else (f" [edge {issue.edge_index}]" if issue.edge_index is not None else "")
        field = f" ({issue.path})" if issue.path else ""
        print(f"  {issue.severity.value:<7} {issue.code}{where}{field}: {issue.message}", file=stream)


# ---- serve ---------------------------------------------------------------------------------
def cmd_serve(argv: list[str]) -> int:
    import uvicorn

    from .api.app import create_app
    from .lifecycle import watch_parent
    from .startup import open_storage
    from .storage import StorageError

    parser = argparse.ArgumentParser(prog="botfoundry serve", description="Start the local engine.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="Folder for the database (default: a per-user BotFoundry folder).")
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="Exit automatically when this process no longer exists (set by the desktop app).")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except RuntimeError as exc:
        print(f"BotFoundry cannot start: {exc}", file=sys.stderr, flush=True)
        return EXIT_USAGE

    data_dir = args.data_dir or default_data_dir()
    try:
        storage, interrupted = open_storage(data_dir)
    except StorageError as exc:
        print(f"BotFoundry cannot start: {exc}", file=sys.stderr, flush=True)
        return EXIT_USAGE
    print(f"Database ready in {data_dir} (schema v{storage.schema_version}); "
          f"{len(interrupted)} interrupted run(s) recovered.", flush=True)

    app = create_app(settings, storage)
    # Always loopback. Never bind 0.0.0.0.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="info"))

    if args.parent_pid is not None:
        def parent_gone() -> None:
            print(f"Parent process {args.parent_pid} is gone; shutting down.", flush=True)
            server.should_exit = True

        watch_parent(args.parent_pid, parent_gone)

    try:
        server.run()  # on shutdown the API ends active runs in a recorded state before we get here
    finally:
        storage.close()
    return EXIT_OK


# ---- nodes ---------------------------------------------------------------------------------
def cmd_nodes(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="botfoundry nodes", description="List the available node types.")
    parser.add_argument("--json", action="store_true", help="Print the full catalog as JSON.")
    args = parser.parse_args(argv)
    registry = build_default_registry()
    if args.json:
        print(json.dumps(registry.catalog(), indent=2))
        return EXIT_OK
    for spec in registry.all():
        ports = f"in: {', '.join(p.name for p in spec.inputs) or '-'}  out: {', '.join(p.name for p in spec.outputs) or '-'}"
        print(f"{spec.id:<22} v{spec.version}  {spec.category.value:<8} {spec.title:<16} {ports}")
    return EXIT_OK


# ---- validate ------------------------------------------------------------------------------
def cmd_validate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="botfoundry validate", description="Check a workflow file.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--json", action="store_true", help="Print the validation result as JSON.")
    args = parser.parse_args(argv)
    _, result = parse_and_validate(_load_json(args.file), build_default_registry())
    if args.json:
        print(result.model_dump_json(indent=2))
    elif result.issues:
        print(f"{args.file}: {len(result.errors)} error(s), {len(result.warnings)} warning(s)")
        _print_issues(result)
    else:
        print(f"{args.file}: valid")
    if result.ok and result.required_permissions and not args.json:
        print(f"  permissions needed: {', '.join(result.required_permissions)}")
    return EXIT_OK if result.ok else EXIT_FAILED


# ---- run -----------------------------------------------------------------------------------
def _event_line(event: RunEvent) -> str:
    p = event.payload
    detail = ""
    if event.type == EventType.NODE_COMPLETED:
        detail = p.get("summary", "")
    elif event.type == EventType.NODE_FAILED:
        detail = f"{p['error'].get('code')}: {p['error'].get('message')}"
    elif event.type == EventType.NODE_SKIPPED:
        detail = p.get("reason", "")
    elif event.type == EventType.NODE_PROGRESS:
        detail = p.get("message", "")
    elif event.type == EventType.RUN_FAILED:
        detail = (p.get("error") or {}).get("message", "")
    elif event.type == EventType.RUN_CANCELLED:
        detail = p.get("reason", "")
    elif event.type == EventType.VALIDATION_FAILED:
        detail = f"{len(p.get('issues', []))} issue(s)"
    stamp = event.timestamp.astimezone().strftime("%H:%M:%S")
    return f"{stamp}  {event.type.value:<20} {event.node_id or '':<14} {detail}".rstrip()


def cmd_run(argv: list[str]) -> int:
    from .storage import Storage, StorageError

    parser = argparse.ArgumentParser(prog="botfoundry run", description="Run a workflow file.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--data-dir", type=Path, default=None, help="Where to save the run (default: per-user folder).")
    parser.add_argument("--no-save", action="store_true", help="Do not write anything to the database.")
    parser.add_argument("--json", action="store_true", help="One JSON event per line, then a final result line.")
    args = parser.parse_args(argv)

    registry = build_default_registry()
    definition, result = parse_and_validate(_load_json(args.file), registry)
    if definition is None or not result.ok:
        print(f"{args.file} is not valid, so nothing was run:", file=sys.stderr)
        _print_issues(result, sys.stderr)
        return EXIT_FAILED

    run = Run(definition, registry)
    storage = None
    persister = None
    if not args.no_save:
        data_dir = args.data_dir or default_data_dir()
        try:
            # NOTE: deliberately no crash recovery here. Only the engine process does that, so a CLI
            # run can never mark another process's active runs as interrupted.
            storage = Storage.open(data_dir / DATABASE_FILE)
            persister = storage.attach(run)
        except StorageError as exc:
            print(f"Cannot save the run: {exc}\n(use --no-save to run without saving)", file=sys.stderr)
            return EXIT_USAGE

    show: Callable[[RunEvent], None] = (
        (lambda e: print(e.model_dump_json(), flush=True)) if args.json else (lambda e: print(_event_line(e), flush=True)))
    for event in run.events.all():
        show(event)
    run.events.subscribe(show)

    interrupted = False
    try:
        asyncio.run(run.execute())
    except KeyboardInterrupt:  # Ctrl+C: asyncio cancels the run, which records itself as cancelled
        interrupted = True
    finally:
        if storage is not None:
            storage.close()

    if not run.is_finished:  # defensive: never report a run as finished when it is not
        print("The run did not finish.", file=sys.stderr)
        return EXIT_FAILED
    if args.json:
        print(json.dumps({"result": run.snapshot()}), flush=True)
    else:
        print(f"\nRun {run.run_id}: {run.status.value}" + (f"  (saved to {data_dir / DATABASE_FILE})" if storage else ""))
        for record in run.nodes.values():
            note = record.summary or record.reason or (record.error or {}).get("message", "")
            print(f"  {record.node_id:<14} {record.status.value:<10} {note}")
    if persister is not None and persister.errors:
        print(f"WARNING: {len(persister.errors)} problem(s) saving this run's history; it may be incomplete.", file=sys.stderr)
    if run.status == RunStatus.CANCELLED or interrupted:
        return EXIT_CANCELLED
    return EXIT_OK if run.status == RunStatus.COMPLETED else EXIT_FAILED


COMMANDS: dict[str, Callable[[list[str]], int]] = {
    "serve": cmd_serve, "nodes": cmd_nodes, "validate": cmd_validate, "run": cmd_run}

_OVERVIEW = __doc__


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in COMMANDS:
        command, rest = args[0], args[1:]
    elif args and args[0] in ("-h", "--help"):
        print(_OVERVIEW)
        return EXIT_OK
    else:
        command, rest = "serve", args  # no subcommand: behave exactly like the original `botfoundry --port ...`
    try:
        return COMMANDS[command](rest)
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE
