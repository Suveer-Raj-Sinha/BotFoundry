"""Approval requests: a run pauses until a person says yes or no (no answer in time = no)."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"


class ApprovalError(Exception):
    pass


class ApprovalNotFound(ApprovalError):
    pass


class ApprovalNotPending(ApprovalError):
    """Already answered (or the run has ended)."""


@dataclass
class Approval:
    approval_id: str
    run_id: str
    node_id: str
    kind: str  # "node_permissions" (the engine asked) or "explicit" (a node asked)
    summary: str
    details: dict[str, Any]
    requested_at: datetime
    expires_at: datetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    reason: str | None = None  # engine outcome code: approved, denied, timeout, cancelled, run_failed, interrupted
    note: str | None = None  # free text the person typed (never interpreted by the engine)
    decided_at: datetime | None = None
    future: Any = field(default=None, repr=False)  # asyncio.Future the waiting run sleeps on

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id, "run_id": self.run_id, "node_id": self.node_id, "kind": self.kind,
            "summary": self.summary, "details": self.details, "status": self.status.value,
            "requested_at": self.requested_at.isoformat(), "expires_at": self.expires_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None, "reason": self.reason, "note": self.note,
        }
