"""Request and response models (the source of truth for the future TypeScript types)."""
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..rules.evaluate import ActionDecision
from ..rules.models import Rulebook
from ..workflow.events import RunEvent
from ..workflow.validation import ValidationResult


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class NodeCatalog(BaseModel):
    nodes: list[dict[str, Any]]


class WorkflowSummary(BaseModel):
    id: str
    name: str
    description: str = ""
    node_count: int
    created_at: str
    updated_at: str


class WorkflowList(BaseModel):
    workflows: list[WorkflowSummary]


class WorkflowSaved(BaseModel):
    workflow: WorkflowSummary
    validation: ValidationResult


class Deleted(BaseModel):
    deleted: bool


class RunStart(BaseModel):
    """Start a run from a saved workflow (`workflow_id`) or an inline `definition`; exactly one."""
    workflow_id: str | None = None
    definition: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "RunStart":
        if (self.workflow_id is None) == (self.definition is None):
            raise ValueError("provide exactly one of 'workflow_id' or 'definition'")
        return self


class NodeRunOut(BaseModel):
    node_id: str
    type: str
    status: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    error: dict[str, Any] | None = None
    reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class ApprovalOut(BaseModel):
    approval_id: str
    run_id: str
    node_id: str | None = None
    kind: str | None = None  # "node_permissions" (the engine asked) or "explicit" (a node asked)
    summary: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    status: str  # pending | granted | denied
    requested_at: str
    expires_at: str | None = None  # no answer by then = denied
    decided_at: str | None = None
    reason: str | None = None  # approved, denied, timeout, cancelled, run_failed, interrupted
    note: str | None = None  # what the person typed


class PendingApprovalOut(ApprovalOut):
    workflow_id: str
    workflow_name: str


class ApprovalList(BaseModel):
    approvals: list[ApprovalOut]


class PendingApprovalList(BaseModel):
    approvals: list[PendingApprovalOut]


class ApprovalDecision(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=500)


class RunSummaryOut(BaseModel):
    run_id: str
    workflow_id: str
    workflow_name: str
    status: str
    error: dict[str, Any] | None = None
    rulebook_bot_id: str | None = None
    rulebook_version: int | None = None
    created_at: str
    finished_at: str | None = None


class RunDetailOut(RunSummaryOut):
    nodes: list[NodeRunOut]
    active: bool
    pending_approvals: list[ApprovalOut] = Field(default_factory=list)
    persistence_errors: list[str] = Field(default_factory=list)  # non-empty = history may be incomplete


class RunList(BaseModel):
    runs: list[RunSummaryOut]


class EventsPage(BaseModel):
    events: list[RunEvent]
    next_after: int  # pass as ?after= to get the next page
    has_more: bool  # more events are already stored beyond this page
    run_status: str
    finished: bool  # the run reached a terminal state (when also has_more is false, polling can stop)


class CancelOut(BaseModel):
    run_id: str
    cancel_requested: bool


class PermissionOut(BaseModel):
    name: str
    description: str
    risk: str
    system_ceiling: str  # the most any Rulebook can grant


class PermissionList(BaseModel):
    permissions: list[PermissionOut]


class RulebookSummary(BaseModel):
    bot_id: str
    name: str
    version: int
    created_at: str


class RulebookList(BaseModel):
    rulebooks: list[RulebookSummary]


class RulebookVersionInfo(BaseModel):
    version: int
    name: str
    note: str | None = None
    created_at: str


class RulebookVersionList(BaseModel):
    bot_id: str
    versions: list[RulebookVersionInfo]


class RulebookOut(BaseModel):
    rulebook: Rulebook
    version: int
    created_at: str
    note: str | None = None


class RulebookSaved(BaseModel):
    version: int
    created: bool  # false when the content was identical to the latest version
    rulebook: Rulebook
    validation: ValidationResult  # warnings are allowed; errors prevent saving


class EvaluateRequest(BaseModel):
    permission: str
    paths: list[str] = Field(default_factory=list, max_length=1000)


class EvaluateOut(BaseModel):
    bot_id: str
    version: int
    decision: ActionDecision
