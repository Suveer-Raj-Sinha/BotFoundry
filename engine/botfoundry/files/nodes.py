"""The read-only file nodes. Only scan and hash touch the disk, and both go through the engine:
`ctx.authorize("fs.read", paths)` checks the Rulebook and returns the resolved paths to read."""
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..workflow.node import Category, NodeContext, NodeError, NodeResult, NodeSpec
from ..workflow.types import DataType, PortSpec
from .blocking import run_blocking
from .duplicates import KEEP_STRATEGIES, NotHashed, find_duplicates
from .filtering import normalize_extension, run_filter
from .hasher import hash_items
from .items import check_items, format_size
from .scanner import RootUnreadable, ScanOptions, TooManyFiles, scan_folder


def _items(inputs: dict[str, Any], who: str) -> list[dict[str, Any]]:
    try:
        return check_items(inputs["items"], who)
    except ValueError as exc:
        raise NodeError("files.bad_input", str(exc)) from exc


# ---- files.scan_folder --------------------------------------------------------------------
class ScanConfig(BaseModel):
    folder: str = Field(min_length=1, description="Absolute path of the folder to scan (must be an approved folder).")
    recursive: bool = True
    max_depth: int | None = Field(default=None, ge=0, le=1000, description="0 = only the folder itself.")
    include_hidden: bool = False
    max_items: int = Field(default=100_000, ge=1, le=1_000_000)


_SKIP_REASONS = {"path.forbidden": "forbidden by the bot's Rulebook", "path.protected": "protected system location",
                 "path.outside_allowed": "outside the approved folders", "path.escapes_via_link": "leads outside the approved folders"}


async def _scan(ctx: NodeContext, config: ScanConfig, inputs: dict[str, Any]) -> NodeResult:
    [root] = ctx.authorize("fs.read", [config.folder])

    def allow_dir(path: str) -> str | None:
        try:
            ctx.authorize("fs.read", [path])
        except NodeError as err:
            if err.code.startswith("path."):  # a subfolder we may not enter: skip it, and say so
                return _SKIP_REASONS.get(err.code, err.code)
            raise
        return None

    options = ScanOptions(config.recursive, config.max_depth, config.include_hidden, config.max_items)
    try:
        result = await run_blocking(ctx, lambda stop: scan_folder(
            root, options, allow_dir=allow_dir, stop=stop, progress=lambda m: ctx.report_progress(None, m)))
    except RootUnreadable as exc:
        missing = isinstance(exc.cause, (FileNotFoundError, NotADirectoryError))
        raise NodeError("scan.folder_not_found" if missing else "scan.folder_unreadable",
                        f"Cannot scan '{config.folder}': {exc.cause.strerror or exc.cause}.",
                        {"folder": config.folder}) from exc
    except TooManyFiles as exc:
        raise NodeError("scan.too_many_files", f"The folder has more than {exc.limit} files. Raise max_items or "
                        "scan a smaller folder.", {"max_items": exc.limit}) from exc
    stats = result["stats"]
    return NodeResult(outputs=result, summary=f"Found {stats['files']} files ({format_size(stats['bytes'])}) in "
                      f"{stats['folders']} folders; skipped {stats['skipped']}")


SCAN_FOLDER = NodeSpec(
    id="files.scan_folder", version=1, title="Scan Folder",
    description="Lists the files in an approved folder (read-only). Links, junctions, cloud placeholders and "
                "forbidden subfolders are skipped and reported, never followed.",
    category=Category.FILES, config_model=ScanConfig,
    inputs=(PortSpec(name="start", required=False, description="Optional: connect the trigger here."),),
    outputs=(PortSpec(name="items", type=DataType.LIST, description="File records."),
             PortSpec(name="skipped", type=DataType.LIST, description="Things not scanned, with the reason."),
             PortSpec(name="stats", type=DataType.OBJECT)),
    handler=_scan, permissions=frozenset({"fs.read"}), timeout_seconds=600)


# ---- files.hash ---------------------------------------------------------------------------
class HashConfig(BaseModel):
    algorithm: Literal["sha256"] = "sha256"
    skip_unique_sizes: bool = Field(default=False, description="Skip files whose size is unique: they cannot have "
                                    "a duplicate. Much faster for duplicate finding; those files get no hash.")


async def _hash(ctx: NodeContext, config: HashConfig, inputs: dict[str, Any]) -> NodeResult:
    items = _items(inputs, "files.hash")

    def work(stop):
        resolved: list[str] = []
        for start in range(0, len(items), 500):  # the engine re-checks every path it is about to read
            stop.check()
            resolved += ctx.authorize("fs.read", [i["path"] for i in items[start:start + 500]])
        return hash_items(items, resolved, config.algorithm, skip_unique_sizes=config.skip_unique_sizes,
                          stop=stop, progress=ctx.report_progress)

    result = await run_blocking(ctx, work)
    stats = result["stats"]
    note = f"; {stats['failed']} could not be read" if stats["failed"] else ""
    return NodeResult(outputs=result, summary=f"Hashed {stats['hashed']} files ({format_size(stats['bytes_read'])}); "
                      f"{stats['not_needed']} did not need it{note}")


HASH = NodeSpec(
    id="files.hash", version=1, title="File Hash",
    description="Reads each file and adds a content hash (read-only). Files that cannot be read or that change "
                "while being read are reported in 'failed', never guessed.",
    category=Category.FILES, config_model=HashConfig,
    inputs=(PortSpec(name="items", type=DataType.LIST),),
    outputs=(PortSpec(name="items", type=DataType.LIST), PortSpec(name="failed", type=DataType.LIST),
             PortSpec(name="stats", type=DataType.OBJECT)),
    handler=_hash, permissions=frozenset({"fs.read"}), timeout_seconds=3600)


# ---- files.filter -------------------------------------------------------------------------
class FilterConfig(BaseModel):
    match: Literal["all", "any"] = "all"
    extensions: list[str] | None = None
    name_contains: list[str] | None = None
    name_regex: str | None = Field(default=None, max_length=200)
    min_size: int | None = Field(default=None, ge=0)
    max_size: int | None = Field(default=None, ge=0)
    older_than_days: float | None = Field(default=None, ge=0)
    newer_than_days: float | None = Field(default=None, ge=0)
    negate: bool = False
    reference_time: datetime | None = Field(default=None, description="'Now' for age tests (default: the real time).")

    @field_validator("extensions")
    @classmethod
    def _extensions(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else [normalize_extension(v) for v in value]

    @field_validator("name_regex")
    @classmethod
    def _regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"not a valid regular expression: {exc}")
        return value

    @model_validator(mode="after")
    def _at_least_one_criterion(self) -> "FilterConfig":
        if not self.criteria():
            raise ValueError("give at least one criterion (extensions, name_contains, name_regex, sizes or ages)")
        if self.min_size is not None and self.max_size is not None and self.min_size > self.max_size:
            raise ValueError("min_size is larger than max_size")
        return self

    def criteria(self) -> dict[str, Any]:
        keys = ("extensions", "name_contains", "min_size", "max_size", "older_than_days", "newer_than_days")
        found = {k: getattr(self, k) for k in keys if getattr(self, k) is not None}
        if self.name_regex is not None:
            found["name_regex"] = re.compile(self.name_regex, re.IGNORECASE)
        return found


async def _filter(ctx: NodeContext, config: FilterConfig, inputs: dict[str, Any]) -> NodeResult:
    items = _items(inputs, "files.filter")
    matched, rejected = run_filter(items, config.criteria(), config.match, config.negate, config.reference_time)
    return NodeResult(outputs={"matched": matched, "rejected": rejected,
                               "stats": {"matched": len(matched), "rejected": len(rejected)}},
                      summary=f"{len(matched)} matched, {len(rejected)} did not")


FILTER = NodeSpec(
    id="files.filter", version=1, title="Filter Items",
    description="Splits file records into 'matched' and 'rejected' by extension, name, size or age. Works on "
                "the records only; it never touches the disk.",
    category=Category.DATA, config_model=FilterConfig,
    inputs=(PortSpec(name="items", type=DataType.LIST),),
    outputs=(PortSpec(name="matched", type=DataType.LIST), PortSpec(name="rejected", type=DataType.LIST),
             PortSpec(name="stats", type=DataType.OBJECT)),
    handler=_filter, timeout_seconds=60)


# ---- files.find_duplicates ----------------------------------------------------------------
class DuplicatesConfig(BaseModel):
    keep: Literal["oldest", "newest", "shortest_path", "first"] = "oldest"
    ignore_empty: bool = True


async def _duplicates(ctx: NodeContext, config: DuplicatesConfig, inputs: dict[str, Any]) -> NodeResult:
    items = _items(inputs, "files.find_duplicates")
    try:
        result = find_duplicates(items, config.keep, config.ignore_empty)
    except NotHashed as exc:
        raise NodeError("duplicates.not_hashed", str(exc)) from exc
    stats = result["stats"]
    return NodeResult(outputs=result, summary=f"{stats['groups']} groups of duplicates, {stats['duplicate_files']} "
                      f"redundant files, {format_size(stats['wasted_bytes'])} wasted")


FIND_DUPLICATES = NodeSpec(
    id="files.find_duplicates", version=1, title="Find Duplicates",
    description="Groups files with identical content (needs File Hash first) and suggests which copy to keep. "
                "It only reports; it never deletes or moves anything.",
    category=Category.DATA, config_model=DuplicatesConfig,
    inputs=(PortSpec(name="items", type=DataType.LIST),),
    outputs=(PortSpec(name="groups", type=DataType.LIST), PortSpec(name="duplicates", type=DataType.LIST),
             PortSpec(name="stats", type=DataType.OBJECT)),
    handler=_duplicates, timeout_seconds=60)

FILE_NODES = (SCAN_FOLDER, HASH, FILTER, FIND_DUPLICATES)
