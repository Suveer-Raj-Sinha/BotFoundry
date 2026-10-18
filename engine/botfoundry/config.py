"""Runtime configuration. The desktop shell passes the token via environment."""
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Origins allowed to call the API from a browser context:
# Vite dev server, and the Tauri webview on Windows / other platforms.
DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
)

TOKEN_HEADER = "X-BotFoundry-Token"


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("BOTFOUNDRY_TOKEN", "")
        if len(token) < 16:
            raise RuntimeError(
                "BOTFOUNDRY_TOKEN must be set to a random string of at least 16 characters."
            )
        return cls(token=token)


DATABASE_FILE = "botfoundry.db"


def default_data_dir() -> Path:
    """Where BotFoundry keeps its database when no --data-dir is given.
    (The desktop app always passes its own folder explicitly.)"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "BotFoundry"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "botfoundry"
