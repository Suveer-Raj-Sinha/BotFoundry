"""Run the engine: python -m botfoundry --port 8765  (token from BOTFOUNDRY_TOKEN)."""
import argparse

import uvicorn

from .api.app import create_app
from .config import Settings
from .lifecycle import watch_parent


def main() -> None:
    parser = argparse.ArgumentParser(prog="botfoundry")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help="Exit automatically when this process no longer exists (set by the desktop app).",
    )
    args = parser.parse_args()

    app = create_app(Settings.from_env())
    # Always loopback. Never bind 0.0.0.0.
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="info")
    server = uvicorn.Server(config)

    if args.parent_pid is not None:

        def parent_gone() -> None:
            print(f"Parent process {args.parent_pid} is gone; shutting down.", flush=True)
            server.should_exit = True

        watch_parent(args.parent_pid, parent_gone)

    server.run()


if __name__ == "__main__":
    main()
