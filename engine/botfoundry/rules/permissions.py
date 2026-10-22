"""The permission vocabulary: every permission a node may declare and a Rulebook may grant."""
from enum import Enum
from typing import Iterable

from pydantic import BaseModel


class Policy(str, Enum):
    DENY = "deny"
    ASK = "ask"  # allowed only with explicit approval for each use
    ALLOW = "allow"


_ORDER = {Policy.DENY: 0, Policy.ASK: 1, Policy.ALLOW: 2}


def most_restrictive(*policies: Policy) -> Policy:
    return min(policies, key=_ORDER.__getitem__)


def exceeds(policy: Policy, ceiling: Policy) -> bool:
    return _ORDER[policy] > _ORDER[ceiling]


class PermissionInfo(BaseModel):
    name: str
    description: str
    risk: str  # low | medium | high
    system_ceiling: Policy  # the most a Rulebook can ever grant, whatever it says


# System safety constraints (they outrank every Rulebook):
#  - deleting files and running commands are not available to bots in this version,
#  - network access is not available,
#  - running tests (Phase 3+) can only ever be granted per use, never blanket-allowed.
PERMISSIONS: dict[str, PermissionInfo] = {p.name: p for p in (
    PermissionInfo(name="fs.read", description="Read file and folder listings inside approved folders.",
                   risk="low", system_ceiling=Policy.ALLOW),
    PermissionInfo(name="fs.write", description="Create or change files inside approved folders.",
                   risk="medium", system_ceiling=Policy.ALLOW),
    PermissionInfo(name="fs.move", description="Move or rename files inside approved folders.",
                   risk="medium", system_ceiling=Policy.ALLOW),
    PermissionInfo(name="fs.delete", description="Delete files. Not available to bots in this version.",
                   risk="high", system_ceiling=Policy.DENY),
    PermissionInfo(name="git.read", description="Inspect git repositories inside approved folders.",
                   risk="low", system_ceiling=Policy.ALLOW),
    PermissionInfo(name="process.run_tests", description="Run a project's tests. Always needs approval.",
                   risk="high", system_ceiling=Policy.ASK),
    PermissionInfo(name="network.access", description="Use the network. Not available to bots in this version.",
                   risk="high", system_ceiling=Policy.DENY),
)}


def known_permission_names(extra: Iterable[str] = ()) -> set[str]:
    """The built-in vocabulary plus any permission that registered nodes declare."""
    return set(PERMISSIONS) | set(extra)
