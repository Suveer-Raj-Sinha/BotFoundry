"""Small, careful filesystem helpers shared by the file nodes."""
import os
import threading
from typing import Callable

REPARSE_POINT = 0x400  # Windows: symlinks, junctions, mount points, OneDrive/cloud placeholders
HIDDEN = 0x2
SYSTEM = 0x4
_LONG_PATH_AT = 240


class Stopped(Exception):
    """The work was cancelled (run cancelled, node timed out, or the engine is stopping)."""


def to_extended_path(path: str, *, windows: bool | None = None) -> str:
    """On Windows, give long paths the \\\\?\\ prefix so they work without the system long-path setting.
    Other platforms (and short paths) are returned unchanged."""
    if windows is None:
        windows = os.name == "nt"
    if not windows or len(path) < _LONG_PATH_AT or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def attributes(entry: "os.DirEntry[str]") -> int:
    """Windows file attributes (0 on other systems)."""
    try:
        return getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return 0


def is_link_or_reparse(entry: "os.DirEntry[str]") -> bool:
    """A symlink, or on Windows any reparse point (junction, mount point, OneDrive placeholder).
    These are never followed or read: following could leave the approved folder, and reading a cloud
    placeholder would silently download it."""
    return entry.is_symlink() or bool(attributes(entry) & REPARSE_POINT)


def is_hidden(entry: "os.DirEntry[str]") -> bool:
    return entry.name.startswith(".") or bool(attributes(entry) & (HIDDEN | SYSTEM))


class StopFlag:
    """Cooperative cancellation for work running in a worker thread."""

    def __init__(self, *extra: Callable[[], bool]) -> None:
        self._event = threading.Event()
        self._extra = extra

    def set(self) -> None:
        self._event.set()

    def __call__(self) -> bool:
        return self._event.is_set() or any(check() for check in self._extra)

    def check(self) -> None:
        if self():
            raise Stopped()
