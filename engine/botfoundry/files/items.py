"""The file record that flows between the file nodes (plain JSON-friendly dicts)."""
from datetime import datetime, timezone
from typing import Any

# A file item:
#   path       absolute path (already authorized by the engine)
#   name       file name with extension          stem       name without the extension
#   extension  lower-case with the dot ("" if none)
#   size       bytes                             modified   ISO-8601 UTC timestamp, e.g. 2026-01-02T03:04:05Z
#   hash, hash_algorithm   ADDED BY files.hash: the hex digest, or None (with hash_algorithm None) when the file was
#              deliberately not read because its size is unique. A record with no 'hash' key was never hashed.

REQUIRED_FIELDS = ("path", "name", "size")


def iso_utc(timestamp: float) -> str | None:
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def check_items(items: Any, where: str) -> list[dict[str, Any]]:
    """Make sure a port really carries a list of file records; raises ValueError with a helpful message."""
    if not isinstance(items, list):
        raise ValueError(f"{where}: expected a list of file records, got {type(items).__name__}.")
    for index, item in enumerate(items):
        if not isinstance(item, dict) or any(f not in item for f in REQUIRED_FIELDS):
            raise ValueError(f"{where}: item {index} is not a file record (needs {', '.join(REQUIRED_FIELDS)}).")
        if not isinstance(item["path"], str) or not isinstance(item["size"], int) or isinstance(item["size"], bool):
            raise ValueError(f"{where}: item {index} has a bad path or size.")
    return items


def format_size(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{count} B"
