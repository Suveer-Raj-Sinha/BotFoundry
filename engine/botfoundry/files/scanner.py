"""Walk a folder and describe the files in it. Read-only and safe by design:
  * links, junctions and cloud placeholders are never followed or read (they are reported as skipped);
  * hidden and system entries are skipped unless asked for;
  * a subfolder the Rulebook forbids is skipped whole (and reported), not scanned;
  * an unreadable folder or file is reported and the scan continues; nothing is silently dropped;
  * the result order is deterministic."""
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from .fsutil import StopFlag, is_hidden, is_link_or_reparse, to_extended_path
from .items import iso_utc


class RootUnreadable(Exception):
    """The folder being scanned itself cannot be opened (missing, not a folder, or no permission)."""

    def __init__(self, path: str, cause: OSError) -> None:
        super().__init__(f"{path}: {cause.strerror or cause}")
        self.path = path
        self.cause = cause


class TooManyFiles(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"More than {limit} files.")
        self.limit = limit


@dataclass(frozen=True)
class ScanOptions:
    recursive: bool = True
    max_depth: int | None = None  # 0 = only the folder itself
    include_hidden: bool = False
    max_items: int = 100_000


def scan_folder(root: str, options: ScanOptions, *, allow_dir: Callable[[str], str | None],
                stop: StopFlag, progress: Callable[[str], None] = lambda message: None) -> dict[str, Any]:
    """`allow_dir(path)` returns None to allow a subfolder or a short reason to skip it."""
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    hidden_skipped = 0
    folders = 0
    total_bytes = 0
    last_report = time.monotonic()
    stack: list[tuple[str, int]] = [(root, 0)]

    while stack:
        stop.check()
        directory, depth = stack.pop()
        try:
            with os.scandir(to_extended_path(directory)) as listing:
                entries = sorted(listing, key=lambda e: (e.name.casefold(), e.name))
        except OSError as exc:
            if directory == root:  # a scan of a folder we cannot open is a failure, not an empty result
                raise RootUnreadable(directory, exc) from exc
            skipped.append({"path": directory, "reason": f"unreadable folder: {exc.strerror or exc}"})
            continue
        folders += 1
        subfolders: list[tuple[str, int]] = []
        for entry in entries:
            stop.check()
            full = os.path.join(directory, entry.name)
            try:
                if is_link_or_reparse(entry):
                    skipped.append({"path": full, "reason": "link or reparse point (not followed)"})
                    continue
                if not options.include_hidden and is_hidden(entry):
                    hidden_skipped += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if options.recursive and (options.max_depth is None or depth < options.max_depth):
                        reason = allow_dir(full)
                        if reason is None:
                            subfolders.append((full, depth + 1))
                        else:
                            skipped.append({"path": full, "reason": reason})
                    continue
                if not entry.is_file(follow_symlinks=False):
                    skipped.append({"path": full, "reason": "not a regular file"})
                    continue
                info = entry.stat(follow_symlinks=False)
                stem, extension = os.path.splitext(entry.name)
                items.append({"path": full, "name": entry.name, "stem": stem, "extension": extension.lower(),
                              "size": info.st_size, "modified": iso_utc(info.st_mtime)})
                total_bytes += info.st_size
                if len(items) > options.max_items:
                    raise TooManyFiles(options.max_items)
            except OSError as exc:
                skipped.append({"path": full, "reason": f"unreadable: {exc.strerror or exc}"})
        stack.extend(reversed(subfolders))  # so folders are visited in sorted order
        now = time.monotonic()
        if now - last_report >= 0.25:
            last_report = now
            progress(f"{len(items)} files in {folders} folders so far")

    items.sort(key=lambda i: (i["path"].casefold(), i["path"]))
    return {"items": items, "skipped": skipped,
            "stats": {"files": len(items), "bytes": total_bytes, "folders": folders,
                      "skipped": len(skipped), "hidden_skipped": hidden_skipped}}
