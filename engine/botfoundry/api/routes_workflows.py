from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from ..workflow.validation import ValidationResult, parse_and_validate
from .deps import get_registry, get_storage
from .errors import ApiError
from .schemas import Deleted, WorkflowList, WorkflowSaved
from .security import require_token

router = APIRouter(dependencies=[Depends(require_token)])


@router.post("/workflows/validate", response_model=ValidationResult)
def validate_workflow_document(request: Request, body: dict[str, Any] = Body(...)) -> ValidationResult:
    """Check a workflow without saving or running it. Always 200: problems are in the result."""
    _, result = parse_and_validate(body, get_registry(request))
    return result


@router.get("/workflows", response_model=WorkflowList)
def list_workflows(request: Request) -> dict[str, Any]:
    return {"workflows": get_storage(request).list_workflows()}


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
    definition = get_storage(request).get_workflow(workflow_id)
    if definition is None:
        raise ApiError(404, "workflow_not_found", f"No saved workflow with id '{workflow_id}'.")
    return definition.model_dump(mode="json")


@router.put("/workflows/{workflow_id}", response_model=WorkflowSaved)
def save_workflow(workflow_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Create or replace a workflow. A well-formed workflow is saved even if it does not validate yet
    (work in progress); the validation result is returned so the editor can show what is left to fix."""
    definition, result = parse_and_validate(body, get_registry(request))
    if definition is None:
        raise ApiError(422, "workflow_invalid_structure", "The workflow document is not well formed.",
                       {"issues": [i.model_dump(mode="json") for i in result.issues]})
    if definition.id != workflow_id:
        raise ApiError(422, "id_mismatch",
                       f"The URL says '{workflow_id}' but the workflow's id is '{definition.id}'.")
    summary = get_storage(request).save_workflow(definition)
    return {"workflow": summary, "validation": result}


@router.delete("/workflows/{workflow_id}", response_model=Deleted)
def delete_workflow(workflow_id: str, request: Request) -> Deleted:
    """Deletes the saved workflow only; past runs keep their own copy."""
    if not get_storage(request).delete_workflow(workflow_id):
        raise ApiError(404, "workflow_not_found", f"No saved workflow with id '{workflow_id}'.")
    return Deleted(deleted=True)
