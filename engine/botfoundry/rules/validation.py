"""Validating a Rulebook before it can be saved: structure, unknown permissions, conflicts with the
system constraints, and settings that make no sense together."""
from typing import Any, Iterable

from pydantic import ValidationError

from ..workflow.validation import Severity, ValidationIssue, ValidationResult
from .constraints import SystemConstraints
from .models import Rulebook
from .paths import PathError, contains
from .permissions import Policy, exceeds


def _folder(style, raw: str):
    try:
        return style.normalize(raw), None
    except PathError as err:
        return None, err


def validate_rulebook(rulebook: Rulebook, constraints: SystemConstraints,
                      known_permissions: Iterable[str]) -> ValidationResult:
    issues: list[ValidationIssue] = []
    known = set(known_permissions)
    style = constraints.path_style()

    def add(severity: Severity, code: str, message: str, path: str | None = None) -> None:
        issues.append(ValidationIssue(code=code, severity=severity, message=message, path=path))

    error = lambda code, msg, path=None: add(Severity.ERROR, code, msg, path)  # noqa: E731
    warn = lambda code, msg, path=None: add(Severity.WARNING, code, msg, path)  # noqa: E731

    protected = [n for raw in constraints.protected_paths if (n := _folder(style, raw)[0]) is not None]
    too_broad = [n for raw in constraints.too_broad_paths if (n := _folder(style, raw)[0]) is not None]

    allowed = []
    for i, raw in enumerate(rulebook.allowed_folders):
        where = f"allowed_folders.{i}"
        n, err = _folder(style, raw)
        if err:
            error("rulebook.folder_invalid", f"'{raw}': {err.message}", where)
            continue
        if n.is_root or any(n.key_root == b.key_root and n.key_parts == b.key_parts for b in too_broad):
            error("rulebook.folder_too_broad", f"'{raw}' is too broad (a drive root or a whole user profile). "
                  "Approve a specific folder instead.", where)
            continue
        hit = next((p for p in protected if contains(p, n)), None)
        if hit:
            error("rulebook.folder_protected", f"'{raw}' is inside the protected location '{hit}'.", where)
            continue
        hit = next((p for p in protected if contains(n, p)), None)
        if hit:
            error("rulebook.folder_protected", f"'{raw}' contains the protected location '{hit}'.", where)
            continue
        if any(a.key_root == n.key_root and a.key_parts == n.key_parts for _, a in allowed):
            warn("rulebook.folder_duplicate", f"'{raw}' is listed more than once.", where)
            continue
        outer = next((a for _, a in allowed if contains(a, n)), None)
        if outer:
            warn("rulebook.folder_nested", f"'{raw}' is already covered by '{outer}'.", where)
        allowed.append((raw, n))

    forbidden = []
    for i, raw in enumerate(rulebook.forbidden_folders):
        where = f"forbidden_folders.{i}"
        n, err = _folder(style, raw)
        if err:
            error("rulebook.folder_invalid", f"'{raw}': {err.message}", where)
            continue
        forbidden.append((raw, n))
        if not any(contains(a, n) for _, a in allowed):
            warn("rulebook.forbidden_outside", f"'{raw}' is not inside any approved folder, so it has no effect.", where)
    for raw_a, a in allowed:
        covering = next((raw_f for raw_f, f in forbidden if contains(f, a)), None)
        if covering:
            error("rulebook.folder_conflict", f"The approved folder '{raw_a}' lies entirely inside the forbidden "
                  f"folder '{covering}', so it could never be used.")

    for name, policy in rulebook.permissions.items():
        where = f"permissions.{name}"
        if name not in known:
            error("rulebook.unknown_permission", f"'{name}' is not a known permission (typo?). Known: {', '.join(sorted(known))}.", where)
            continue
        ceiling = constraints.ceilings.get(name, Policy.ALLOW)
        if exceeds(policy, ceiling):
            error("rulebook.exceeds_system_limit",
                  f"'{name}' cannot be '{policy.value}': the system limit for it is '{ceiling.value}'.", where)

    if rulebook.default_policy == Policy.ALLOW:
        warn("rulebook.permissive_default", "default_policy is 'allow', so any permission you did not list is granted "
             "(system limits still apply). 'deny' is safer.", "default_policy")
    grants_files = any(p != Policy.DENY and n.startswith("fs.") for n, p in rulebook.permissions.items()) or (
        rulebook.default_policy != Policy.DENY)
    if grants_files and not rulebook.allowed_folders:
        warn("rulebook.no_folders", "File permissions are granted but no folder is approved, so every file action will be denied.")
    changes_files = any(rulebook.permissions.get(n, rulebook.default_policy) != Policy.DENY
                        for n in ("fs.write", "fs.move") if constraints.ceilings.get(n, Policy.ALLOW) != Policy.DENY)
    if changes_files and rulebook.limits.max_files_per_run is None:
        warn("rulebook.no_file_limit", "Files can be changed but limits.max_files_per_run is not set.", "limits.max_files_per_run")

    return ValidationResult(issues=issues, required_permissions=[])


def parse_and_validate_rulebook(data: Any, constraints: SystemConstraints,
                                known_permissions: Iterable[str]) -> tuple[Rulebook | None, ValidationResult]:
    try:
        rulebook = Rulebook.model_validate(data)
    except ValidationError as exc:
        issues = [ValidationIssue(code="schema.invalid", severity=Severity.ERROR, message=e["msg"],
                                  path=".".join(str(p) for p in e["loc"])) for e in exc.errors()]
        return None, ValidationResult(issues=issues)
    return rulebook, validate_rulebook(rulebook, constraints, known_permissions)
