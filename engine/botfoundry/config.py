"""Runtime configuration. The desktop shell passes the token via environment."""
from dataclasses import dataclass
import os

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
