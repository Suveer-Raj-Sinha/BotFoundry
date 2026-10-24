import copy

import pytest

from botfoundry.rules.constraints import SystemConstraints, default_constraints
from botfoundry.rules.evaluate import evaluate_action
from botfoundry.rules.models import Rulebook
from botfoundry.rules.paths import WINDOWS
from botfoundry.rules.permissions import PERMISSIONS, Policy, known_permission_names
from botfoundry.rules.validation import parse_and_validate_rulebook, validate_rulebook
from botfoundry.workflow.validation import Severity

CONSTRAINTS = SystemConstraints(
    protected_paths=("C:\\Windows", "C:\\Program Files", "C:\\Data\\BotFoundry"),
    too_broad_paths=("C:\\Users\\Me",), style=WINDOWS)
KNOWN = known_permission_names()
DOWNLOADS = "C:\\Users\\Me\\Downloads"


def good():
    return {"bot_id": "downloads-cleaner", "name": "Downloads cleaner", "allowed_folders": [DOWNLOADS],
            "forbidden_folders": [DOWNLOADS + "\\Keep"],
            "permissions": {"fs.read": "allow", "fs.move": "ask"}, "limits": {"max_files_per_run": 500}}


def check(data, constraints=CONSTRAINTS, known=KNOWN):
    rulebook, result = parse_and_validate_rulebook(data, constraints, known)
    return rulebook, result, {i.code for i in result.issues}


def with_changes(**changes):
    data = good()
    data.update(changes)
    return data


# ---- validation ---------------------------------------------------------------------------
def test_a_good_rulebook_validates_cleanly():
    rulebook, result, codes = check(good())
    assert rulebook is not None and result.ok and result.issues == []


@pytest.mark.parametrize("data,path", [
    ({**good(), "surprise": 1}, "surprise"),
    ({**good(), "bot_id": "bad id!"}, "bot_id"),
    ({**good(), "name": ""}, "name"),
    ({**good(), "default_policy": "maybe"}, "default_policy"),
    ({**good(), "permissions": {"fs.read": "sometimes"}}, "permissions.fs.read"),
    ({**good(), "approval": {"timeout_seconds": 1}}, "approval.timeout_seconds"),
    ({**good(), "limits": {"max_files_per_run": 0}}, "limits.max_files_per_run"),
    ({**good(), "schema_version": 2}, "schema_version"),
])
def test_structural_problems_are_reported_with_their_location(data, path):
    rulebook, result, codes = check(data)
    assert rulebook is None and codes == {"schema.invalid"}
    assert path in {i.path for i in result.issues}


@pytest.mark.parametrize("folder,code", [
    ("relative\\folder", "rulebook.folder_invalid"), ("C:\\a\\NUL", "rulebook.folder_invalid"),
    ("C:\\", "rulebook.folder_too_broad"), ("\\\\srv\\share", "rulebook.folder_too_broad"),
    ("C:\\Users\\Me", "rulebook.folder_too_broad"), ("c:/users/me/", "rulebook.folder_too_broad"),
    ("C:\\Windows", "rulebook.folder_protected"), ("C:\\Windows\\Temp", "rulebook.folder_protected"),
    ("C:\\Data", "rulebook.folder_protected"),                           # contains a protected folder
    ("c:\\data\\botfoundry\\sub", "rulebook.folder_protected"),          # BotFoundry's own data
])
def test_dangerous_or_broken_approved_folders_are_rejected(folder, code):
    _, result, codes = check(with_changes(allowed_folders=[folder], forbidden_folders=[]))
    assert code in codes and not result.ok


def test_folder_overlaps_and_conflicts():
    _, result, codes = check(with_changes(allowed_folders=[DOWNLOADS, DOWNLOADS.lower(), DOWNLOADS + "\\Sub"],
                                          forbidden_folders=[]))
    assert {"rulebook.folder_duplicate", "rulebook.folder_nested"} <= codes and result.ok   # warnings only
    _, result, codes = check(with_changes(forbidden_folders=["C:\\Elsewhere"]))
    assert "rulebook.forbidden_outside" in codes and result.ok
    _, result, codes = check(with_changes(forbidden_folders=["C:\\Users\\Me"]))
    assert "rulebook.folder_conflict" in codes and not result.ok            # the whole approved folder is forbidden


def test_permission_checks():
    _, result, codes = check(with_changes(permissions={"fs.mvoe": "allow"}))
    assert codes == {"rulebook.unknown_permission"} and not result.ok
    for permission, policy in (("fs.delete", "allow"), ("fs.delete", "ask"), ("network.access", "allow"),
                               ("process.run_tests", "allow")):
        _, result, codes = check(with_changes(permissions={permission: policy}))
        assert "rulebook.exceeds_system_limit" in codes and not result.ok, (permission, policy)
    _, result, _ = check(with_changes(permissions={"process.run_tests": "ask", "fs.delete": "deny"}))
    assert result.ok                                                         # at or below the ceiling is fine


def test_node_declared_permissions_are_accepted_when_known():
    data = with_changes(permissions={"custom.thing": "allow"})
    assert "rulebook.unknown_permission" in check(data)[2]
    assert check(data, known=known_permission_names(["custom.thing"]))[1].ok


def test_warnings_for_risky_but_legal_settings():
    _, result, codes = check(with_changes(default_policy="allow"))
    assert "rulebook.permissive_default" in codes and result.ok
    _, _, codes = check(with_changes(allowed_folders=[], forbidden_folders=[]))
    assert "rulebook.no_folders" in codes
    _, _, codes = check(with_changes(limits={}))
    assert "rulebook.no_file_limit" in codes
    assert all(i.severity == Severity.WARNING for i in check(with_changes(limits={}))[1].issues)


def test_canonical_json_ignores_ordering_and_defaults():
    a = Rulebook.model_validate(good())
    shuffled = dict(reversed(list(good().items())))
    b = Rulebook.model_validate({**shuffled, "permissions": dict(reversed(list(good()["permissions"].items())))})
    assert a.canonical_json() == b.canonical_json()
    assert a.canonical_json() != Rulebook.model_validate(with_changes(name="Other")).canonical_json()


def test_validation_never_mutates_its_input():
    data = good()
    before = copy.deepcopy(data)
    check(data)
    assert data == before


# ---- evaluation ---------------------------------------------------------------------------
RULEBOOK = Rulebook.model_validate(good())


def decide(permission, paths=(), rulebook=RULEBOOK, constraints=CONSTRAINTS, **kw):
    return evaluate_action(rulebook, constraints, permission, paths, resolve=False, **kw)


def test_allow_ask_and_deny_outcomes():
    inside = [DOWNLOADS + "\\a.txt", DOWNLOADS + "\\sub\\b.txt"]
    allowed = decide("fs.read", inside)
    assert allowed.outcome == Policy.ALLOW and allowed.code == "ok" and len(allowed.paths) == 2
    asked = decide("fs.move", inside)
    assert asked.outcome == Policy.ASK and asked.code == "approval.required"
    unlisted = decide("fs.write", inside)
    assert unlisted.outcome == Policy.DENY and unlisted.code == "rulebook.not_granted"
    explicit = decide("fs.read", inside, rulebook=Rulebook.model_validate(with_changes(permissions={"fs.read": "deny"})))
    assert explicit.outcome == Policy.DENY and explicit.code == "rulebook.denied"


def test_system_limits_beat_the_rulebook_even_if_it_was_never_validated():
    risky = Rulebook.model_validate(with_changes(permissions={"fs.delete": "allow", "process.run_tests": "allow",
                                                              "network.access": "allow"}))
    delete = decide("fs.delete", [DOWNLOADS + "\\a.txt"], rulebook=risky)
    assert delete.outcome == Policy.DENY and delete.code == "system.limit"
    assert decide("network.access", rulebook=risky).code == "system.limit"
    tests = decide("process.run_tests", [DOWNLOADS], rulebook=risky)
    assert tests.outcome == Policy.ASK                       # "allow" is capped at "ask" by the system


def test_paths_must_all_be_authorized():
    d = decide("fs.read", [DOWNLOADS + "\\ok.txt", "C:\\Users\\Me\\Documents\\x.txt", "C:\\Windows\\y"])
    assert d.outcome == Policy.DENY and d.code == "path.outside_allowed" and "1 more" in d.message
    assert [p.allowed for p in d.paths] == [True, False, False]
    assert decide("fs.read", [DOWNLOADS + "\\Keep\\secret.txt"]).code == "path.forbidden"
    assert decide("fs.read", [DOWNLOADS + "\\NUL"]).code == "path.reserved_name"
    assert decide("fs.move", ["C:\\Users\\Me\\Downloads\\..\\Documents\\x"]).outcome == Policy.DENY


def test_path_denial_applies_even_when_the_action_would_need_approval():
    assert decide("fs.move", ["C:\\Elsewhere\\x"]).outcome == Policy.DENY        # never asks to approve the impossible


def test_protected_paths_cannot_be_reached_even_if_approved_in_the_rulebook():
    sneaky = Rulebook.model_validate(with_changes(allowed_folders=["C:\\Windows\\Temp"], forbidden_folders=[],
                                                  permissions={"fs.read": "allow"}))   # would fail validation
    assert decide("fs.read", ["C:\\Windows\\Temp\\x"], rulebook=sneaky).code == "path.protected"


def test_no_rulebook_means_no_permission():
    d = evaluate_action(None, CONSTRAINTS, "fs.read", [DOWNLOADS], resolve=False)
    assert d.outcome == Policy.DENY and d.code == "rulebook.missing"


def test_unknown_permissions_are_denied():
    assert decide("fs.teleport").code == "permission.unknown"
    assert decide("custom.thing", known_permissions=["custom.thing"]).code == "rulebook.not_granted"


def test_default_policy_allow_grants_unlisted_permissions_but_not_past_system_limits():
    open_book = Rulebook.model_validate(with_changes(default_policy="allow", permissions={}))
    assert decide("fs.write", [DOWNLOADS + "\\a"], rulebook=open_book).outcome == Policy.ALLOW
    assert decide("fs.delete", rulebook=open_book).outcome == Policy.DENY


def test_actions_without_paths_are_judged_on_policy_alone():
    assert decide("git.read", rulebook=Rulebook.model_validate(with_changes(permissions={"git.read": "allow"}))).outcome == Policy.ALLOW


def test_decisions_are_json_friendly():
    import json
    json.dumps(decide("fs.read", [DOWNLOADS + "\\a"]).model_dump(mode="json"))


def test_permission_vocabulary_is_consistent():
    assert PERMISSIONS["fs.delete"].system_ceiling == Policy.DENY
    assert PERMISSIONS["process.run_tests"].system_ceiling == Policy.ASK
    assert all(p.name == key and p.description and p.risk in ("low", "medium", "high") for key, p in PERMISSIONS.items())


def test_default_constraints_protect_the_data_folder(tmp_path):
    constraints = default_constraints(data_dir=tmp_path / "BotFoundryData")
    assert str((tmp_path / "BotFoundryData").resolve()) in constraints.protected_paths
    assert constraints.ceilings["fs.delete"] == Policy.DENY
