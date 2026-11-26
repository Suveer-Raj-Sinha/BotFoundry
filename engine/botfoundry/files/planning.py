"""Planning moves (Phase 3.2): decide where each file WOULD go. This module changes nothing.

`build_plan` is a pure function. Everything that touches the outside world is passed in:
  check         asks the engine whether the Rulebook allows a path (see NodeContext.preflight)
  exists        does a path already exist?
  folder_state  "dir", "missing" (can be created) or "blocked" (a file is in the way)
so the whole thing can be tested with Windows path rules on any machine.

Rules the plan follows:
  * Never overwrite. A destination that exists, or that two files both want, means the file is skipped
    and reported. When two files want the same destination NEITHER moves (no arbitrary winner).
  * Never trust a record: every source and destination is checked against the Rulebook, and the name
    that is moved comes from the path itself.
  * Same input, same plan, whatever the input order (`plan_id` proves it).
"""
import hashlib
import json
import posixpath
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from ..rules.paths import RESERVED_NAMES, NPath, PathError, contains
from ..workflow.node import PathVerdict
from .filtering import matches
from .fsutil import StopFlag
from .items import parse_iso

PLAN_VERSION = 1
DATE_MODES = ("none", "year", "year_month")

# Built-in categories: folder name -> extensions. Used after any rules the bot's workflow gives.
DEFAULT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Images": (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".svg"),
    "Documents": (".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md"),
    "Spreadsheets": (".xls", ".xlsx", ".ods", ".csv"),
    "Presentations": (".ppt", ".pptx", ".odp"),
    "Archives": (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"),
    "Audio": (".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma"),
    "Video": (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v"),
    "Installers": (".exe", ".msi", ".msix"),
}

_BAD_NAME_CHARS = set('<>:"|?*')
_MAX_SEGMENT = 100
_MAX_DEPTH = 8


class PlanConfigError(ValueError):
    """The planning settings cannot work (a bad folder name, an unusable destination)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_folder(text: str) -> tuple[str, ...]:
    """A folder name RELATIVE to the destination folder ('Images' or 'Work/Invoices') as its parts.
    Raises ValueError with a plain-language reason. Checked the same way on every platform, because a plan
    made anywhere may be carried out on Windows."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("the folder name is empty")
    raw = text.strip()
    if raw[0] in "/\\" or re.match(r"^[A-Za-z]:", raw):
        raise ValueError(f"'{text}' must be a folder name inside the destination folder, not an absolute path")
    parts: list[str] = []
    for segment in re.split(r"[\\/]", raw):
        if segment == "":
            continue
        if segment in (".", ".."):
            raise ValueError("'.' and '..' are not allowed in folder names")
        if len(segment) > _MAX_SEGMENT:
            raise ValueError(f"'{segment[:20]}...' is longer than {_MAX_SEGMENT} characters")
        if any(ord(c) < 32 or c in _BAD_NAME_CHARS for c in segment):
            raise ValueError(f"'{segment}' contains characters that are not allowed ({''.join(sorted(_BAD_NAME_CHARS))})")
        if segment != segment.rstrip(" ."):
            raise ValueError(f"'{segment}' ends with a dot or a space, which Windows does not allow")
        if segment.split(".")[0].rstrip(" ").upper() in RESERVED_NAMES:
            raise ValueError(f"'{segment}' is a reserved Windows device name")
        parts.append(segment)
    if not parts:
        raise ValueError("the folder name is empty")
    if len(parts) > _MAX_DEPTH:
        raise ValueError(f"more than {_MAX_DEPTH} levels deep")
    return tuple(parts)


@dataclass(frozen=True)
class Rule:
    """First matching rule wins. `criteria` is the same dict the Filter node uses."""
    label: str
    folder: tuple[str, ...]
    criteria: dict[str, Any]
    mode: str = "all"
    negate: bool = False


def default_rules() -> list[Rule]:
    return [Rule(label=f"default: {name}", folder=(name,), criteria={"extensions": list(extensions)})
            for name, extensions in DEFAULT_CATEGORIES.items()]


@dataclass(frozen=True)
class PlanOptions:
    destination_root: str
    rules: tuple[Rule, ...]
    unmatched_folder: tuple[str, ...] | None = None  # None: files no rule matches stay where they are
    date_folders: str = "none"
    now: datetime | None = None


def _join(style: Any, base: NPath, parts: Sequence[str]) -> NPath:
    return style.normalize(str(base) + base.sep + base.sep.join(parts))


def _date_parts(mode: str, modified: str | None) -> tuple[str, ...] | None:
    """UTC year or year-month folders. None when the file's date is unknown."""
    if mode == "none":
        return ()
    stamp = parse_iso(modified)
    if stamp is None:
        return None
    stamp = stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp
    return (f"{stamp.year:04d}",) if mode == "year" else (f"{stamp.year:04d}-{stamp.month:02d}",)


def plan_fingerprint(root: str, moves: Sequence[dict[str, Any]]) -> str:
    """Identifies exactly this plan. Approval of a plan should be tied to this value, so that approving one
    plan can never authorize a different one."""
    body = [{"source": m["source"], "destination": m["destination"], "size": m["size"],
             "modified": m.get("modified"), "hash": m.get("hash")} for m in moves]
    text = json.dumps({"version": PLAN_VERSION, "root": root, "moves": body}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_plan(items: Sequence[dict[str, Any]], options: PlanOptions, *, style: Any,
               check: Callable[[list[str]], list[PathVerdict]],
               exists: Callable[[str], bool],
               folder_state: Callable[[str], str],
               stop: StopFlag | None = None) -> dict[str, Any]:
    stop = stop or StopFlag()
    if options.date_folders not in DATE_MODES:
        raise PlanConfigError("plan.bad_setting", f"date_folders must be one of {DATE_MODES}.")
    now = options.now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    try:
        root = style.normalize(options.destination_root)
    except PathError as err:
        raise PlanConfigError("plan.bad_destination", f"The destination folder cannot be used: {err.message}") from err
    for rule in options.rules:  # a bad folder name is a settings error, not thousands of skipped files
        try:
            _join(style, root, rule.folder)
        except PathError as err:
            raise PlanConfigError("plan.bad_folder", f"Rule '{rule.label}': {err.message}") from err
    if options.unmatched_folder:
        try:
            _join(style, root, options.unmatched_folder)
        except PathError as err:
            raise PlanConfigError("plan.bad_folder", f"unmatched_folder: {err.message}") from err

    skipped: list[dict[str, Any]] = []

    def skip(path: str, code: str, reason: str) -> None:
        skipped.append({"path": path, "code": code, "reason": reason})

    # 1. classify every file and work out where it would go ------------------------------------------------
    candidates: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda i: (i["path"].casefold(), i["path"])):
        stop.check()
        raw = item["path"]
        try:
            src = style.normalize(raw)
        except PathError as err:
            skip(raw, "source.invalid", f"The file's path cannot be used: {err.message}")
            continue
        if not src.parts:
            skip(raw, "source.invalid", "The path is a drive or share, not a file.")
            continue
        name = src.parts[-1]
        view = {"name": name, "extension": posixpath.splitext(name)[1].lower(), "size": item["size"],
                "modified": item.get("modified")}
        chosen: Rule | None = None
        for rule in options.rules:
            if matches(view, rule.criteria, rule.mode, now) != rule.negate:
                chosen = rule
                break
        if chosen is not None:
            folder, label = chosen.folder, chosen.label
        elif options.unmatched_folder:
            folder, label = options.unmatched_folder, "unmatched"
        else:
            skip(raw, "rule.no_match", "No rule matches this file, so it stays where it is.")
            continue
        dated = _date_parts(options.date_folders, item.get("modified"))
        if dated is None:
            skip(raw, "date.unknown", "The file's modified date is unknown, so it cannot go in a dated folder.")
            continue
        try:
            dest = _join(style, root, (*folder, *dated, name))
        except PathError as err:
            skip(raw, "destination.invalid", f"The destination name cannot be used: {err.message}")
            continue
        if (dest.key_root, dest.key_parts) == (src.key_root, src.key_parts):
            skip(raw, "already_in_place", "Already where it belongs.")
            continue
        candidates.append({"item": item, "src": src, "dest": dest, "rule": label, "category": "/".join(folder)})

    # 2. ask the Rulebook about every source and destination (one question per path, answers in order) --------
    if candidates:
        paths: list[str] = []
        for c in candidates:
            paths += [str(c["src"]), str(c["dest"])]
        verdicts = check(paths)
        if len(verdicts) != len(paths):
            raise PlanConfigError("plan.preflight_failed", "The Rulebook check returned a different number of "
                                  "answers than questions; refusing to build a plan.")
        allowed: list[dict[str, Any]] = []
        for index, c in enumerate(candidates):
            source_verdict, dest_verdict = verdicts[2 * index], verdicts[2 * index + 1]
            if not source_verdict.allowed:
                skip(c["item"]["path"], source_verdict.code, f"Not allowed to move this file: {source_verdict.message}")
            elif not dest_verdict.allowed:
                skip(c["item"]["path"], dest_verdict.code, f"Not allowed to move it there: {dest_verdict.message}")
            else:
                allowed.append(c)
        candidates = allowed

    # 3. two files that want the same destination: neither moves -------------------------------------------
    wanted: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        wanted[(c["dest"].key_root, c["dest"].key_parts)].append(c)
    survivors: list[dict[str, Any]] = []
    for c in candidates:
        group = wanted[(c["dest"].key_root, c["dest"].key_parts)]
        if len(group) == 1:
            survivors.append(c)
            continue
        others = [str(o["src"]) for o in group if o is not c]
        shown = ", ".join(others[:3]) + (f" and {len(others) - 3} more" if len(others) > 3 else "")
        skip(c["item"]["path"], "destination.collision",
             f"{len(others)} other file(s) would also be moved to {c['dest']} ({shown}); none of them is moved.")

    # 4. never overwrite what is already there; never move into a folder blocked by a file -----------------
    folders: dict[str, str] = {}
    moves: list[dict[str, Any]] = []
    new_folders: set[str] = set()
    for c in survivors:
        stop.check()
        dest: NPath = c["dest"]
        parent = str(replace(dest, parts=dest.parts[:-1], key_parts=dest.key_parts[:-1]))
        if exists(str(dest)):
            skip(c["item"]["path"], "destination.exists", f"{dest} already exists; nothing is ever overwritten.")
            continue
        state = folders.get(parent)
        if state is None:
            state = folders[parent] = folder_state(parent)
        if state == "blocked":
            skip(c["item"]["path"], "destination.folder_blocked",
                 f"A file is in the way of the folder {parent}, so nothing can be moved into it.")
            continue
        if state == "missing":
            new_folders.add(parent)
        move = {"source": c["item"]["path"], "destination": str(dest), "name": dest.parts[-1],
                "size": c["item"]["size"], "modified": c["item"].get("modified"),
                "category": c["category"], "rule": c["rule"]}
        if "hash" in c["item"]:
            move["hash"] = c["item"]["hash"]
            move["hash_algorithm"] = c["item"].get("hash_algorithm")
        moves.append(move)

    skipped.sort(key=lambda s: (s["path"].casefold(), s["path"], s["code"]))
    by_category = Counter(m["category"] for m in moves)
    stats = {"files_considered": len(items), "moves": len(moves), "skipped": len(skipped),
             "bytes_to_move": sum(m["size"] for m in moves), "folders_to_create": len(new_folders),
             "by_category": dict(sorted(by_category.items())),
             "skipped_by_reason": dict(sorted(Counter(s["code"] for s in skipped).items()))}
    plan = {"version": PLAN_VERSION, "plan_id": plan_fingerprint(str(root), moves), "destination_root": str(root),
            "moves": moves, "folders_to_create": sorted(new_folders, key=lambda f: (f.casefold(), f)),
            "approval_required": False, "warnings": []}
    return {"plan": plan, "skipped": skipped, "stats": stats}
