"""Content hashing. Reads only; never modifies a file."""
import hashlib
import os
import time
from collections import Counter
from typing import Any, Callable

from .fsutil import StopFlag, to_extended_path

CHUNK = 1024 * 1024
ALGORITHMS = {"sha256": hashlib.sha256}


def hash_file(path: str, algorithm: str, stop: StopFlag, expected_size: int | None = None) -> str:
    """Hash one file. Raises OSError for unreadable files and ValueError if the file changed while it was read."""
    real = to_extended_path(path)
    before = os.stat(real)
    if expected_size is not None and before.st_size != expected_size:
        raise ValueError("changed since it was scanned (size differs)")
    digest = ALGORITHMS[algorithm]()
    with open(real, "rb") as handle:
        while True:
            stop.check()
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    after = os.stat(real)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("changed while it was being read")
    return digest.hexdigest()


def hash_items(items: list[dict[str, Any]], resolved_paths: list[str], algorithm: str, *, skip_unique_sizes: bool,
               stop: StopFlag, progress: Callable[[float | None, str], None] = lambda f, m: None) -> dict[str, Any]:
    """`resolved_paths[i]` is the authorized, resolved path to READ for `items[i]`."""
    sizes = Counter(item["size"] for item in items)
    to_read = [i for i, item in enumerate(items) if not skip_unique_sizes or sizes[item["size"]] > 1]
    total = sum(items[i]["size"] for i in to_read) or 1
    done_bytes, last_report = 0, time.monotonic()
    hashed: dict[int, str] = {}
    failed: list[dict[str, str]] = []
    for i in to_read:
        stop.check()
        item = items[i]
        try:
            hashed[i] = hash_file(resolved_paths[i], algorithm, stop, item["size"])
        except (OSError, ValueError) as exc:
            reason = ("vanished" if isinstance(exc, FileNotFoundError) else
                      "permission denied" if isinstance(exc, PermissionError) else
                      str(exc.strerror or exc) if isinstance(exc, OSError) else str(exc))
            failed.append({"path": item["path"], "reason": reason})
        done_bytes += item["size"]
        now = time.monotonic()
        if now - last_report >= 0.25:
            last_report = now
            progress(min(done_bytes / total, 1.0), f"hashed {len(hashed)} of {len(to_read)} files")
    failed_paths = {f["path"] for f in failed}
    out = []
    for i, item in enumerate(items):
        if item["path"] in failed_paths:
            continue
        out.append({**item, "hash": hashed.get(i), "hash_algorithm": algorithm if i in hashed else None})
    return {"items": out, "failed": failed,
            "stats": {"hashed": len(hashed), "not_needed": len(items) - len(to_read), "failed": len(failed),
                      "bytes_read": sum(items[i]["size"] for i in hashed)}}
