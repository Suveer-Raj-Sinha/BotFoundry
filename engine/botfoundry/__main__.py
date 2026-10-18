"""Run the engine:
    python -m botfoundry --port 8765 --data-dir <folder>      (token from BOTFOUNDRY_TOKEN)
"""
import argparse
import sys
from pathlib import Path

import uvicorn

from .api.app import create_app
from .config import Settings, default_data_dir
from .lifecycle import watch_parent
from .startup import open_storage
from .storage import StorageError


def main() -> None:
    parser = argparse.ArgumentParser(prog="botfoundry")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="Folder for the database (default: a per-user BotFoundry folder).")
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="Exit automatically when this process no longer exists (set by the desktop app).")
    args = parser.parse_args()

    data_dir = args.data_dir or default_data_dir()
    try:
        storage, interrupted = open_storage(data_dir)
    except StorageError as exc:
        print(f"BotFoundry cannot start: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2)
    print(f"Database ready in {data_dir} (schema v{storage.schema_version}); "
          f"{len(interrupted)} interrupted run(s) recovered.", flush=True)

    app = create_app(Settings.from_env(), storage)
    # Always loopback. Never bind 0.0.0.0.
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="info")
    server = uvicorn.Server(config)

    if args.parent_pid is not None:

        def parent_gone() -> None:
            print(f"Parent process {args.parent_pid} is gone; shutting down.", flush=True)
            server.should_exit = True

        watch_parent(args.parent_pid, parent_gone)

    try:
        server.run()
    finally:
        storage.close()


if __name__ == "__main__":
    main()
