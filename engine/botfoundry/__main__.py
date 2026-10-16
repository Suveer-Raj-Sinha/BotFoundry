"""Run the engine: python -m botfoundry --port 8765  (token from BOTFOUNDRY_TOKEN)."""
import argparse
import uvicorn

from .api.app import create_app
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="botfoundry")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app = create_app(Settings.from_env())
    # Always loopback. Never bind 0.0.0.0.
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
