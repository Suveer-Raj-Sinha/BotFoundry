from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from ..rules.constraints import SystemConstraints
from ..rules.evaluate import evaluate_action
from ..rules.permissions import PERMISSIONS, Policy, known_permission_names
from ..rules.validation import parse_and_validate_rulebook
from ..workflow.validation import ValidationResult
from .deps import get_constraints, get_known_permissions, get_registry, get_storage
from .errors import ApiError
from .schemas import (EvaluateOut, EvaluateRequest, PermissionList, RulebookList, RulebookOut, RulebookSaved,
                      RulebookVersionList)
from .security import require_token

router = APIRouter(dependencies=[Depends(require_token)])


_constraints = get_constraints
_known = get_known_permissions


@router.get("/permissions", response_model=PermissionList)
def list_permissions(request: Request) -> dict[str, Any]:
    """Every permission a node can declare and a Rulebook can grant, with the system limit for each."""
    constraints = _constraints(request)
    items = [{"name": p.name, "description": p.description, "risk": p.risk,
              "system_ceiling": constraints.ceilings.get(p.name, p.system_ceiling).value}
             for p in PERMISSIONS.values()]
    for name in sorted(_known(request) - set(PERMISSIONS)):
        items.append({"name": name, "description": "Declared by a node type.", "risk": "unknown",
                      "system_ceiling": constraints.ceilings.get(name, Policy.ALLOW).value})
    return {"permissions": items}


@router.post("/rulebooks/validate", response_model=ValidationResult)
def validate_rulebook_document(request: Request, body: dict[str, Any] = Body(...)) -> ValidationResult:
    """Check a Rulebook document without saving it. Always 200: problems are in the result."""
    return parse_and_validate_rulebook(body, _constraints(request), _known(request))[1]


@router.get("/rulebooks", response_model=RulebookList)
def list_rulebooks(request: Request) -> dict[str, Any]:
    return {"rulebooks": get_storage(request).list_rulebooks()}


@router.get("/rulebooks/{bot_id}", response_model=RulebookOut)
def get_rulebook(bot_id: str, request: Request) -> dict[str, Any]:
    found = get_storage(request).get_rulebook(bot_id)
    if found is None:
        raise ApiError(404, "rulebook_not_found", f"Bot '{bot_id}' has no Rulebook yet.")
    return found


@router.get("/rulebooks/{bot_id}/versions", response_model=RulebookVersionList)
def list_versions(bot_id: str, request: Request) -> dict[str, Any]:
    versions = get_storage(request).list_rulebook_versions(bot_id)
    if not versions:
        raise ApiError(404, "rulebook_not_found", f"Bot '{bot_id}' has no Rulebook yet.")
    return {"bot_id": bot_id, "versions": versions}


@router.get("/rulebooks/{bot_id}/versions/{version}", response_model=RulebookOut)
def get_version(bot_id: str, version: int, request: Request) -> dict[str, Any]:
    found = get_storage(request).get_rulebook(bot_id, version)
    if found is None:
        raise ApiError(404, "rulebook_not_found", f"Bot '{bot_id}' has no Rulebook version {version}.")
    return found


@router.put("/rulebooks/{bot_id}", response_model=RulebookSaved)
def save_rulebook(bot_id: str, request: Request, body: dict[str, Any] = Body(...),
                  note: str | None = Query(default=None, max_length=500)) -> dict[str, Any]:
    """Save a new version. Unlike workflows, a Rulebook with ANY error is refused: it is a safety control,
    so an invalid one must never become the active one. Saving identical content creates no new version."""
    rulebook, result = parse_and_validate_rulebook(body, _constraints(request), _known(request))
    if rulebook is None or not result.ok:
        raise ApiError(422, "rulebook_invalid", "The Rulebook has errors, so it was not saved.",
                       {"issues": [i.model_dump(mode="json") for i in result.issues]})
    if rulebook.bot_id != bot_id:
        raise ApiError(422, "id_mismatch", f"The URL says '{bot_id}' but the Rulebook's bot_id is '{rulebook.bot_id}'.")
    version, created = get_storage(request).save_rulebook(rulebook, note)
    return {"version": version, "created": created, "rulebook": rulebook, "validation": result}


@router.post("/rulebooks/{bot_id}/evaluate", response_model=EvaluateOut)
def evaluate(bot_id: str, body: EvaluateRequest, request: Request) -> EvaluateOut:
    """Dry run: what would the engine decide for this permission and these paths under the bot's current
    Rulebook? Read-only (it may look at the filesystem to follow links, but never changes anything)."""
    found = get_storage(request).get_rulebook(bot_id)
    if found is None:
        raise ApiError(404, "rulebook_not_found", f"Bot '{bot_id}' has no Rulebook yet.")
    decision = evaluate_action(found["rulebook"], _constraints(request), body.permission, body.paths,
                               known_permissions=_known(request))
    return EvaluateOut(bot_id=bot_id, version=found["version"], decision=decision)
