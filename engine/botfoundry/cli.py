"""Command line interface.

    botfoundry [--port N] [--data-dir D] [--parent-pid P]   start the engine (default; what the desktop app uses)
    botfoundry serve ...                                     same, explicit
    botfoundry nodes [--json]                                list the node types
    botfoundry validate FILE [--json]                        check a workflow file
    botfoundry run FILE [--data-dir D] [--no-save] [--json] [--rulebook R] [--approve ask|yes|no]
                                                             run a workflow file, printing events live
    botfoundry rulebook validate FILE [--json]               check a Rulebook file
    botfoundry rulebook check FILE PERMISSION [PATH ...]     ask what the Rulebook would decide

Exit codes:  0 ok / completed / allowed   1 invalid, failed or denied   2 usage or environment problem
             3 needs approval (rulebook check)   130 cancelled
"""
import argparse
import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from .config import DATABASE_FILE, Settings, default_data_dir
from .workflow.builtin_nodes import build_default_registry
from .workflow.events import EventType, RunEvent
from .workflow.runner import Run, RunStatus
from .workflow.validation import ValidationResult, parse_and_validate

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_CANCELLED = 0, 1, 2, 130


def stdin_is_interactive() -> bool:
    """True only when a person can really answer: standard input is a terminal or console.
    (On Windows, `isatty()` also says True for the empty NUL device, so ask the console itself.)"""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        if os.name != "nt":
            return True
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        return bool(kernel32.GetConsoleMode(msvcrt.get_osfhandle(sys.stdin.fileno()), ctypes.byref(mode)))
    except (AttributeError, ValueError, OSError):
        return False  # no usable stdin: treat as unattended, never as interactive


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
    from .rules.constraints import default_constraints
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

    app = create_app(settings, storage, constraints=default_constraints(data_dir))
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
    elif event.type == EventType.APPROVAL_REQUESTED:
        detail = f"{p.get('approval_id')}: {p.get('summary', '')}"
    elif event.type in (EventType.APPROVAL_GRANTED, EventType.APPROVAL_DENIED):
        detail = f"{p.get('approval_id')} ({p.get('reason')})"
    elif event.type == EventType.VALIDATION_FAILED:
        detail = f"{len(p.get('issues', []))} issue(s)"
    stamp = event.timestamp.astimezone().strftime("%H:%M:%S")
    return f"{stamp}  {event.type.value:<20} {event.node_id or '':<14} {detail}".rstrip()


def cmd_run(argv: list[str]) -> int:
    from .rules.constraints import default_constraints
    from .rules.permissions import known_permission_names
    from .rules.validation import parse_and_validate_rulebook, validate_rulebook
    from .storage import Storage, StorageError

    parser = argparse.ArgumentParser(prog="botfoundry run", description="Run a workflow file.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--data-dir", type=Path, default=None, help="Where to save the run (default: per-user folder).")
    parser.add_argument("--no-save", action="store_true", help="Do not write anything to the database.")
    parser.add_argument("--json", action="store_true", help="One JSON event per line, then a final result line.")
    parser.add_argument("--rulebook", type=Path, default=None,
                        help="Use this Rulebook file instead of the bot's saved one (not saved; recorded without a version).")
    parser.add_argument("--approve", choices=("ask", "yes", "no"), default="ask",
                        help="What to do when the run asks for approval: ask on the terminal (default; 'no' if there is "
                             "no terminal), approve everything, or deny everything.")
    args = parser.parse_args(argv)

    registry = build_default_registry()
    definition, result = parse_and_validate(_load_json(args.file), registry)
    if definition is None or not result.ok:
        print(f"{args.file} is not valid, so nothing was run:", file=sys.stderr)
        _print_issues(result, sys.stderr)
        return EXIT_FAILED

    constraints = default_constraints()
    known = known_permission_names(p for spec in registry.all() for p in spec.permissions)
    storage = None
    data_dir = args.data_dir or default_data_dir()
    if not args.no_save:
        try:
            # NOTE: deliberately no crash recovery here. Only the engine process does that, so a CLI
            # run can never mark another process's active runs as interrupted.
            storage = Storage.open(data_dir / DATABASE_FILE)
        except StorageError as exc:
            print(f"Cannot save the run: {exc}\n(use --no-save to run without saving)", file=sys.stderr)
            return EXIT_USAGE

    rulebook, version = None, None
    try:
        if args.rulebook:
            rulebook, rb_result = parse_and_validate_rulebook(_load_json(args.rulebook), constraints, known)
            if rulebook is None or not rb_result.ok:
                print(f"{args.rulebook} has errors, so nothing was run:", file=sys.stderr)
                _print_issues(rb_result, sys.stderr)
                return EXIT_FAILED
        elif definition.bot_id and storage is not None:
            found = storage.get_rulebook(definition.bot_id)
            if found is not None:
                rb_result = validate_rulebook(found["rulebook"], constraints, known)
                if not rb_result.ok:
                    print(f"Rulebook '{definition.bot_id}' v{found['version']} no longer passes validation:", file=sys.stderr)
                    _print_issues(rb_result, sys.stderr)
                    return EXIT_FAILED
                rulebook, version = found["rulebook"], found["version"]

        run = Run(definition, registry, rulebook=rulebook, rulebook_version=version, constraints=constraints)
        persister = None
        if storage is not None:
            try:
                persister = storage.attach(run)
            except StorageError as exc:
                print(f"Cannot save the run: {exc}\n(use --no-save to run without saving)", file=sys.stderr)
                return EXIT_USAGE

        show: Callable[[RunEvent], None] = (
            (lambda e: print(e.model_dump_json(), flush=True)) if args.json else (lambda e: print(_event_line(e), flush=True)))

        mode = args.approve
        if mode == "ask" and not stdin_is_interactive():
            mode = "no"
            print("No terminal to ask for approval: any approval request will be denied (use --approve yes to allow).",
                  file=sys.stderr)

        def answer(approval_id: str, approve: bool, why: str) -> None:
            try:
                run.respond(approval_id, approve, why)
            except Exception:  # already answered or the run ended meanwhile
                pass

        def ask_on_terminal(event: RunEvent) -> None:
            p = event.payload
            print(f"\nAPPROVAL NEEDED ({event.node_id}): {p['summary']}", file=sys.stderr)
            for key, value in (p.get("details") or {}).items():
                print(f"    {key}: {value}", file=sys.stderr)
            try:
                reply = input(f"Approve? [y/N] (auto-denied in {p['timeout_seconds']:g}s): ")
            except EOFError:
                reply = ""
            answer(p["approval_id"], reply.strip().lower() in ("y", "yes"), "answered on the terminal")

        def on_event(event: RunEvent) -> None:
            show(event)
            if event.type != EventType.APPROVAL_REQUESTED:
                return
            if mode == "yes":
                answer(event.payload["approval_id"], True, "auto-approved with --approve yes")
            elif mode == "no":
                answer(event.payload["approval_id"], False, "auto-denied (--approve no)")
            else:
                threading.Thread(target=ask_on_terminal, args=(event,), daemon=True).start()

        for event in run.events.all():
            on_event(event)
        run.events.subscribe(on_event)

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
        followed = f"  (Rulebook: {run.rulebook_bot_id}" + (f" v{run.rulebook_version})" if run.rulebook_version else ")") if run.rulebook else ""
        print(f"\nRun {run.run_id}: {run.status.value}" + (f"  (saved to {data_dir / DATABASE_FILE})" if storage else "") + followed)
        for record in run.nodes.values():
            note = record.summary or record.reason or (record.error or {}).get("message", "")
            print(f"  {record.node_id:<14} {record.status.value:<10} {note}")
    if persister is not None and persister.errors:
        print(f"WARNING: {len(persister.errors)} problem(s) saving this run's history; it may be incomplete.", file=sys.stderr)
    if run.status == RunStatus.CANCELLED or interrupted:
        return EXIT_CANCELLED
    return EXIT_OK if run.status == RunStatus.COMPLETED else EXIT_FAILED


# ---- rulebook ------------------------------------------------------------------------------
def cmd_rulebook(argv: list[str]) -> int:
    from .rules.constraints import default_constraints
    from .rules.evaluate import evaluate_action
    from .rules.permissions import Policy, known_permission_names
    from .rules.validation import parse_and_validate_rulebook

    parser = argparse.ArgumentParser(prog="botfoundry rulebook", description="Work with Rulebook files.")
    actions = parser.add_subparsers(dest="action", required=True)
    validate = actions.add_parser("validate", help="Check a Rulebook file.")
    validate.add_argument("file", type=Path)
    validate.add_argument("--json", action="store_true")
    check = actions.add_parser("check", help="Ask what the engine would decide for a permission and paths.")
    check.add_argument("file", type=Path)
    check.add_argument("permission")
    check.add_argument("paths", nargs="*")
    check.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    registry = build_default_registry()
    known = known_permission_names(p for spec in registry.all() for p in spec.permissions)
    constraints = default_constraints()
    rulebook, result = parse_and_validate_rulebook(_load_json(args.file), constraints, known)

    if args.action == "validate":
        if args.json:
            print(result.model_dump_json(indent=2))
        elif result.issues:
            print(f"{args.file}: {len(result.errors)} error(s), {len(result.warnings)} warning(s)")
            _print_issues(result)
        else:
            print(f"{args.file}: valid")
        return EXIT_OK if result.ok else EXIT_FAILED

    if rulebook is None or not result.ok:
        print(f"{args.file} has errors; fix them before checking an action:", file=sys.stderr)
        _print_issues(result, sys.stderr)
        return EXIT_FAILED
    decision = evaluate_action(rulebook, constraints, args.permission, args.paths, known_permissions=known)
    if args.json:
        print(decision.model_dump_json(indent=2))
    else:
        print(f"{decision.outcome.value.upper()}: {decision.message}  [{decision.code}]")
        for path in decision.paths:
            print(f"  {'ok  ' if path.allowed else 'DENY'} {path.path}" + ("" if path.allowed else f"  ({path.code})"))
    return {Policy.ALLOW: EXIT_OK, Policy.ASK: 3, Policy.DENY: EXIT_FAILED}[decision.outcome]


COMMANDS: dict[str, Callable[[list[str]], int]] = {
    "serve": cmd_serve, "nodes": cmd_nodes, "validate": cmd_validate, "run": cmd_run, "rulebook": cmd_rulebook}

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
