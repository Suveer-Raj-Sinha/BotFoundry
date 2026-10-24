import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ..rules.validation import validate_rulebook
from ..run_manager import RunNotActive, TooManyActiveRuns
from ..storage.database import StorageError
from ..workflow.approvals import ApprovalNotFound, ApprovalNotPending
from ..workflow.definition import WorkflowDefinition
from ..workflow.validation import parse_and_validate, validate_workflow
from .deps import get_constraints, get_known_permissions, get_manager, get_registry, get_storage
from .errors import ApiError
from .schemas import (ApprovalDecision, ApprovalList, ApprovalOut, CancelOut, EventsPage, PendingApprovalList,
                      RunDetailOut, RunList, RunStart)
from .security import require_token

router = APIRouter(dependencies=[Depends(require_token)])
_TERMINAL = ("completed", "failed", "cancelled")


def _detail(request: Request, run_id: str) -> dict[str, Any]:
    run = get_storage(request).get_run(run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"No run with id '{run_id}'.")
    manager = get_manager(request)
    run["active"] = manager.is_active(run_id)
    run["persistence_errors"] = manager.persistence_errors(run_id)
    run["pending_approvals"] = [a for a in get_storage(request).get_approvals(run_id) if a["status"] == "pending"]
    return run


def _issues(result: Any) -> list[dict[str, Any]]:
    return [i.model_dump(mode="json") for i in result.issues]


@router.post("/runs", status_code=202, response_model=RunDetailOut)
async def start_run(body: RunStart, request: Request) -> dict[str, Any]:
    """Validate, then start a run in the background. Poll `/runs/{id}/events` to follow it."""
    registry, storage, manager = get_registry(request), get_storage(request), get_manager(request)

    if body.workflow_id is not None:
        definition: WorkflowDefinition | None = storage.get_workflow(body.workflow_id)
        if definition is None:
            raise ApiError(404, "workflow_not_found", f"No saved workflow with id '{body.workflow_id}'.")
        result = validate_workflow(definition, registry)
    else:
        definition, result = parse_and_validate(body.definition, registry)
    if definition is None or not result.ok:
        raise ApiError(422, "workflow_invalid", "The workflow has validation errors, so no run was started.",
                       {"issues": _issues(result)})

    # Pin the bot's latest Rulebook for this run, re-checked under today's system rules.
    rulebook, version = None, None
    if definition.bot_id:
        found = storage.get_rulebook(definition.bot_id)
        if found is not None:
            check = validate_rulebook(found["rulebook"], get_constraints(request), get_known_permissions(request))
            if not check.ok:
                raise ApiError(422, "rulebook_invalid",
                               f"Rulebook '{definition.bot_id}' v{found['version']} no longer passes validation under "
                               "the current system rules, so no run was started. Fix and save it again.",
                               {"issues": _issues(check)})
            rulebook, version = found["rulebook"], found["version"]
    if result.required_permissions and rulebook is None:
        why = ("The workflow has no bot_id" if not definition.bot_id else f"Bot '{definition.bot_id}' has no Rulebook")
        raise ApiError(422, "rulebook_required",
                       f"{why}, but its nodes need permissions ({', '.join(result.required_permissions)}). "
                       "Set bot_id and save a Rulebook for that bot first. No run was started.",
                       {"required_permissions": result.required_permissions, "bot_id": definition.bot_id})

    try:
        run = manager.start(definition, rulebook=rulebook, rulebook_version=version)
    except TooManyActiveRuns as exc:
        raise ApiError(429, "too_many_active_runs", str(exc)) from exc
    except (sqlite3.Error, StorageError) as exc:
        raise ApiError(500, "storage_error", f"The run could not be recorded, so it was not started: {exc}") from exc
    return _detail(request, run.run_id)


@router.get("/runs", response_model=RunList)
def list_runs(request: Request, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
              workflow_id: str | None = None) -> dict[str, Any]:
    return {"runs": get_storage(request).list_runs(limit=limit, offset=offset, workflow_id=workflow_id)}


@router.get("/runs/{run_id}", response_model=RunDetailOut)
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    return _detail(request, run_id)


@router.get("/runs/{run_id}/events", response_model=EventsPage)
def get_run_events(run_id: str, request: Request, after: int = Query(0, ge=0),
                   limit: int = Query(1000, ge=1, le=5000)) -> dict[str, Any]:
    """Events with id greater than `after`. The status is read BEFORE the events, so if it says
    `finished` every event up to the end is already included (no race with the final event)."""
    storage = get_storage(request)
    run = storage.get_run(run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"No run with id '{run_id}'.")
    events = storage.get_events(run_id, after_id=after, limit=limit + 1)
    page = events[:limit]
    return {"events": page, "next_after": page[-1].id if page else after, "has_more": len(events) > limit,
            "run_status": run["status"], "finished": run["status"] in _TERMINAL}


@router.post("/runs/{run_id}/cancel", status_code=202, response_model=CancelOut)
async def cancel_run(run_id: str, request: Request) -> CancelOut:
    manager = get_manager(request)
    if manager.cancel(run_id):
        return CancelOut(run_id=run_id, cancel_requested=True)
    if get_storage(request).get_run(run_id) is None:
        raise ApiError(404, "run_not_found", f"No run with id '{run_id}'.")
    raise ApiError(409, "run_not_active", "That run has already finished, so it cannot be cancelled.")


@router.get("/approvals", response_model=PendingApprovalList)
async def list_pending_approvals(request: Request) -> dict[str, Any]:
    """Everything waiting for a person right now, across all active runs (the approvals inbox)."""
    return {"approvals": get_manager(request).pending_approvals()}


@router.get("/runs/{run_id}/approvals", response_model=ApprovalList)
def list_run_approvals(run_id: str, request: Request) -> dict[str, Any]:
    storage = get_storage(request)
    if storage.get_run(run_id) is None:
        raise ApiError(404, "run_not_found", f"No run with id '{run_id}'.")
    return {"approvals": storage.get_approvals(run_id)}


@router.post("/runs/{run_id}/approvals/{approval_id}", response_model=ApprovalOut)
async def answer_approval(run_id: str, approval_id: str, body: ApprovalDecision, request: Request) -> dict[str, Any]:
    """Approve or deny a pending request. The run continues (or fails) immediately."""
    manager, storage = get_manager(request), get_storage(request)
    try:
        approval = manager.respond(run_id, approval_id, body.approve, body.note)
    except RunNotActive:
        if storage.get_run(run_id) is None:
            raise ApiError(404, "run_not_found", f"No run with id '{run_id}'.")
        raise ApiError(409, "run_not_active", "That run has already finished, so nothing is waiting for an answer.")
    except ApprovalNotFound as exc:
        raise ApiError(404, "approval_not_found", str(exc))
    except ApprovalNotPending as exc:
        raise ApiError(409, "approval_already_decided", str(exc))
    return approval.to_dict()
