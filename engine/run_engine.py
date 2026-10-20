"""Entry point used when the engine is frozen into a single executable (PyInstaller)."""
import sys

from botfoundry.cli import main

if __name__ == "__main__":
    sys.exit(main())  # keep the exit code: the desktop app and the build script rely on it
