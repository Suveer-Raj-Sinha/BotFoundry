"""The node contract: what every registered operation must define."""
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Sequence

from pydantic import BaseModel

from .types import PortSpec

NODE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class Category(str, Enum):
    TRIGGER = "trigger"
    LOGIC = "logic"
    DATA = "data"
    FILES = "files"
    APPROVAL = "approval"
    OUTPUT = "output"
    GIT = "git"


class RunCancelled(Exception):
    """Raised inside a node when the run was cancelled."""


class CancelToken:
    """Cooperative cancellation. Long-running handlers should check it regularly."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RunCancelled()


class NodeError(Exception):
    """A structured, expected failure raised by a node handler."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _no_progress(fraction: float | None, message: str) -> None:
    return None


@dataclass
class RunBudget:
    """Per-run limits from the bot's Rulebook that nodes draw on as they work."""
    max_files: int | None = None
    used_files: int = 0

    def consume_files(self, count: int = 1) -> None:
        """Call before touching `count` more files. Raises once the run's file limit would be exceeded."""
        if count < 0:
            raise ValueError("count must not be negative")
        if self.max_files is not None and self.used_files + count > self.max_files:
            raise NodeError("limit.max_files_per_run",
                            f"This run may touch at most {self.max_files} file(s); {self.used_files} already used, "
                            f"{count} more requested.",
                            {"limit": self.max_files, "used": self.used_files, "requested": count})
        self.used_files += count


def _deny_authorize(permission: str, paths: Sequence[str] = ()) -> list[str]:
    raise NodeError("permission.denied", "No Rulebook context is available, so this action is denied.",
                    {"permission": permission})


async def _no_approvals(summary: str, details: dict[str, Any] | None = None, timeout_seconds: float | None = None) -> None:
    raise NodeError("approval.unavailable", "Approvals are not available in this context.")


@dataclass
class NodeContext:
    """What the engine gives a handler while it runs."""

    run_id: str
    node_id: str
    cancel: CancelToken
    report_progress: Callable[[float | None, str], None] = field(default=_no_progress)
    # Ask the engine whether this node may use `permission` on `paths`. Returns the RESOLVED paths to act on
    # (never the text you were given); raises NodeError when the Rulebook or the system limits say no.
    authorize: Callable[[str, Sequence[str]], list[str]] = field(default=_deny_authorize)
    # Pause the run until a person approves; raises NodeError if denied or if nobody answers in time.
    request_approval: Callable[..., Awaitable[None]] = field(default=_no_approvals)
    budget: RunBudget = field(default_factory=RunBudget)


@dataclass(frozen=True)
class NodeResult:
    """Successful result. Output ports missing from `outputs` are INACTIVE:
    nodes connected only to an inactive port are skipped (this is how branching works)."""

    outputs: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


NodeHandler = Callable[[NodeContext, Any, dict[str, Any]], Awaitable[NodeResult]]


@dataclass(frozen=True)
class NodeSpec:
    """The complete, static definition of one node type and version."""

    id: str  # stable identifier, e.g. "core.condition"
    version: int
    title: str
    description: str
    category: Category
    config_model: type[BaseModel]
    inputs: tuple[PortSpec, ...]
    outputs: tuple[PortSpec, ...]
    handler: NodeHandler
    permissions: frozenset[str] = frozenset()
    timeout_seconds: float = 30.0
    cancellable: bool = True

    def __post_init__(self) -> None:
        if not NODE_ID_PATTERN.match(self.id):
            raise ValueError(f"Invalid node id '{self.id}': use lowercase dotted names like 'core.condition'.")
        if self.version < 1:
            raise ValueError(f"{self.id}: version must be >= 1.")
        if self.timeout_seconds <= 0:
            raise ValueError(f"{self.id}: timeout_seconds must be positive.")
        for label, ports in (("input", self.inputs), ("output", self.outputs)):
            names = [p.name for p in ports]
            if len(names) != len(set(names)):
                raise ValueError(f"{self.id}: duplicate {label} port names.")

    def input(self, name: str) -> PortSpec | None:
        return next((p for p in self.inputs if p.name == name), None)

    def output(self, name: str) -> PortSpec | None:
        return next((p for p in self.outputs if p.name == name), None)

    def describe(self) -> dict[str, Any]:
        """JSON-friendly description for the API and the UI node library."""
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "category": self.category.value,
            "config_schema": self.config_model.model_json_schema(),
            "inputs": [p.model_dump(mode="json") for p in self.inputs],
            "outputs": [p.model_dump(mode="json") for p in self.outputs],
            "permissions": sorted(self.permissions),
            "timeout_seconds": self.timeout_seconds,
            "cancellable": self.cancellable,
        }
