"""Deciding whether one action is allowed, needs approval, or is denied.

Order of authority (highest first): system safety constraints  >  the bot's Rulebook.
The result is never more permissive than either."""
from typing import Iterable, Sequence

from pydantic import BaseModel, Field

from .constraints import SystemConstraints
from .models import Rulebook
from .paths import PathDecision, authorize_path
from .permissions import Policy, known_permission_names, most_restrictive


class ActionDecision(BaseModel):
    permission: str
    outcome: Policy  # allow, ask (needs approval) or deny
    code: str
    message: str
    rulebook_policy: Policy | None = None
    system_ceiling: Policy | None = None
    paths: list[PathDecision] = Field(default_factory=list)


def evaluate_action(rulebook: Rulebook | None, constraints: SystemConstraints, permission: str,
                    paths: Sequence[str] = (), *, known_permissions: Iterable[str] = (),
                    resolve: bool = True) -> ActionDecision:
    def result(outcome: Policy, code: str, message: str, **extra) -> ActionDecision:
        return ActionDecision(permission=permission, outcome=outcome, code=code, message=message, **extra)

    if permission not in known_permission_names(known_permissions):
        return result(Policy.DENY, "permission.unknown", f"'{permission}' is not a known permission.")
    ceiling = constraints.ceilings.get(permission, Policy.ALLOW)
    if rulebook is None:
        return result(Policy.DENY, "rulebook.missing",
                      "No Rulebook is assigned to this bot, so this action is denied.", system_ceiling=ceiling)

    listed = permission in rulebook.permissions
    policy = rulebook.permissions.get(permission, rulebook.default_policy)
    effective = most_restrictive(policy, ceiling)
    if effective == Policy.DENY:
        if ceiling == Policy.DENY:
            return result(Policy.DENY, "system.limit", f"'{permission}' is not available to bots (system limit).",
                          rulebook_policy=policy, system_ceiling=ceiling)
        why = "denied by this bot's Rulebook" if listed else "not granted by this bot's Rulebook (default is deny)"
        return result(Policy.DENY, "rulebook.denied" if listed else "rulebook.not_granted",
                      f"'{permission}' is {why}.", rulebook_policy=policy, system_ceiling=ceiling)

    decisions = [authorize_path(p, allowed=rulebook.allowed_folders, forbidden=rulebook.forbidden_folders,
                                protected=constraints.protected_paths, style=constraints.path_style(), resolve=resolve)
                 for p in paths]
    refused = [d for d in decisions if not d.allowed]
    if refused:
        first = refused[0]
        more = f" (and {len(refused) - 1} more)" if len(refused) > 1 else ""
        return result(Policy.DENY, first.code, f"{first.message}{more}", rulebook_policy=policy,
                      system_ceiling=ceiling, paths=decisions)
    if effective == Policy.ASK:
        reason = "the system requires approval for this" if policy != Policy.ASK else "this bot's Rulebook requires approval"
        return result(Policy.ASK, "approval.required", f"'{permission}' needs your approval ({reason}).",
                      rulebook_policy=policy, system_ceiling=ceiling, paths=decisions)
    return result(Policy.ALLOW, "ok", "Allowed.", rulebook_policy=policy, system_ceiling=ceiling, paths=decisions)
