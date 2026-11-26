"""The move planner as a pure function. Paths are Windows-style strings and nothing here touches the disk,
so Windows rules (case-insensitivity, reserved names) are tested on every platform."""
import random
from datetime import datetime, timezone

import pytest

from botfoundry.files.fsutil import StopFlag, Stopped
from botfoundry.files.planning import (DEFAULT_CATEGORIES, PlanConfigError, PlanOptions, Rule, build_plan,
                                       default_rules, parse_folder, plan_fingerprint)
from botfoundry.rules.paths import POSIX, WINDOWS
from botfoundry.workflow.node import PathVerdict

DL = "C:\\Users\\Me\\Downloads"
ROOT = DL + "\\Organized"
NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def rec(path, size=10, modified="2026-03-04T05:06:07Z", **extra):
    name = path.replace("/", "\\").split("\\")[-1]
    return {"path": path, "name": name, "size": size, "modified": modified, **extra}


def allow_all(paths):
    return [PathVerdict(p, True, "ok", "Allowed.") for p in paths]


def options(root=ROOT, rules=None, **changes):
    return PlanOptions(destination_root=root, rules=tuple(default_rules() if rules is None else rules), now=NOW, **changes)


def plan(items, opts=None, *, style=WINDOWS, check=allow_all, exists=lambda p: False, folder_state=lambda p: "missing",
         stop=None):
    return build_plan(items, opts or options(), style=style, check=check, exists=exists, folder_state=folder_state, stop=stop)


def moves(result):
    return {m["source"]: m["destination"] for m in result["plan"]["moves"]}


def skips(result):
    return {s["path"]: s["code"] for s in result["skipped"]}


# ---- classifying ----------------------------------------------------------------------------
def test_files_are_sorted_by_type_whatever_the_case_of_the_extension():
    items = [rec(DL + "\\a.JPG"), rec(DL + "\\b.Pdf"), rec(DL + "\\c.xlsx"), rec(DL + "\\sub\\d.mp3"), rec(DL + "\\e.zip")]
    result = plan(items)
    assert moves(result) == {
        DL + "\\a.JPG": ROOT + "\\Images\\a.JPG", DL + "\\b.Pdf": ROOT + "\\Documents\\b.Pdf",
        DL + "\\c.xlsx": ROOT + "\\Spreadsheets\\c.xlsx", DL + "\\sub\\d.mp3": ROOT + "\\Audio\\d.mp3",
        DL + "\\e.zip": ROOT + "\\Archives\\e.zip"}
    assert result["stats"]["by_category"] == {"Archives": 1, "Audio": 1, "Documents": 1, "Images": 1, "Spreadsheets": 1}
    assert result["skipped"] == []


def test_every_default_category_is_reachable_and_extensions_do_not_overlap():
    seen: dict[str, str] = {}
    for folder, extensions in DEFAULT_CATEGORIES.items():
        for ext in extensions:
            assert ext.startswith(".") and ext == ext.lower()
            assert ext not in seen, f"{ext} is in both {seen.get(ext)} and {folder}"
            seen[ext] = folder
    result = plan([rec(f"{DL}\\f{i}{ext}") for i, ext in enumerate(seen)])
    assert len(result["plan"]["moves"]) == len(seen) and result["skipped"] == []


def test_the_first_matching_rule_wins_and_your_rules_come_before_the_defaults():
    mine = [Rule("invoices", ("Work", "Invoices"), {"name_contains": ["invoice"]}),
            Rule("big pdfs", ("Big",), {"extensions": [".pdf"], "min_size": 1000})]
    items = [rec(DL + "\\Invoice-1.pdf", 5000), rec(DL + "\\huge.pdf", 5000), rec(DL + "\\small.pdf", 5)]
    result = plan(items, options(rules=mine + default_rules()))
    assert moves(result) == {DL + "\\Invoice-1.pdf": ROOT + "\\Work\\Invoices\\Invoice-1.pdf",
                             DL + "\\huge.pdf": ROOT + "\\Big\\huge.pdf", DL + "\\small.pdf": ROOT + "\\Documents\\small.pdf"}
    assert {m["rule"] for m in result["plan"]["moves"]} == {"invoices", "big pdfs", "default: Documents"}
    assert {m["category"] for m in result["plan"]["moves"]} == {"Work/Invoices", "Big", "Documents"}


def test_negated_and_any_mode_rules():
    not_tmp = Rule("keepers", ("Keep",), {"extensions": [".tmp"]}, negate=True)
    either = Rule("either", ("Either",), {"extensions": [".aa"], "name_contains": ["zz"]}, mode="any")
    result = plan([rec(DL + "\\x.tmp"), rec(DL + "\\y.txt"), rec(DL + "\\zz.bin")], options(rules=[either, not_tmp]))
    assert moves(result) == {DL + "\\zz.bin": ROOT + "\\Either\\zz.bin", DL + "\\y.txt": ROOT + "\\Keep\\y.txt"}
    assert skips(result) == {DL + "\\x.tmp": "rule.no_match"}


def test_files_no_rule_matches_stay_put_unless_you_name_a_folder_for_them():
    items = [rec(DL + "\\weird.xyz"), rec(DL + "\\Makefile"), rec(DL + "\\.gitignore"), rec(DL + "\\a.jpg")]
    result = plan(items)
    assert skips(result) == {DL + "\\weird.xyz": "rule.no_match", DL + "\\Makefile": "rule.no_match",
                             DL + "\\.gitignore": "rule.no_match"}
    result = plan(items, options(unmatched_folder=("Misc",)))
    assert result["skipped"] == [] and moves(result)[DL + "\\Makefile"] == ROOT + "\\Misc\\Makefile"
    assert moves(result)[DL + "\\.gitignore"] == ROOT + "\\Misc\\.gitignore"        # a leading dot is not an extension


def test_the_name_and_type_come_from_the_path_not_from_the_record():
    forged = {"path": DL + "\\real.jpg", "name": "innocent.txt", "extension": ".txt", "size": 1, "modified": None}
    result = plan([forged])
    assert moves(result) == {DL + "\\real.jpg": ROOT + "\\Images\\real.jpg"}


# ---- date folders ---------------------------------------------------------------------------
def test_date_folders_use_utc_year_or_year_month():
    items = [rec(DL + "\\a.jpg", modified="2026-12-31T23:30:00Z"), rec(DL + "\\b.jpg", modified="2026-01-01T02:00:00+05:30")]
    by_month = plan(items, options(date_folders="year_month"))
    assert moves(by_month) == {DL + "\\a.jpg": ROOT + "\\Images\\2026-12\\a.jpg",
                               DL + "\\b.jpg": ROOT + "\\Images\\2025-12\\b.jpg"}     # +05:30 is still 2025 in UTC
    by_year = plan(items, options(date_folders="year"))
    assert moves(by_year)[DL + "\\a.jpg"] == ROOT + "\\Images\\2026\\a.jpg"


def test_an_unknown_date_is_reported_not_guessed():
    result = plan([rec(DL + "\\a.jpg", modified=None), rec(DL + "\\b.jpg", modified="garbage")], options(date_folders="year"))
    assert result["plan"]["moves"] == [] and set(skips(result).values()) == {"date.unknown"}
    assert len(plan([rec(DL + "\\a.jpg", modified=None)])["plan"]["moves"]) == 1      # fine when dates are not used


def test_age_rules_use_the_reference_time():
    old = Rule("old", ("Old",), {"older_than_days": 365})
    items = [rec(DL + "\\a.txt", modified="2024-01-01T00:00:00Z"), rec(DL + "\\b.txt", modified="2026-11-01T00:00:00Z")]
    assert moves(plan(items, options(rules=[old]))) == {DL + "\\a.txt": ROOT + "\\Old\\a.txt"}


# ---- never overwrite, never guess -------------------------------------------------------------
def test_a_file_already_in_place_is_left_alone_and_windows_ignores_case():
    result = plan([rec(ROOT + "\\Images\\a.jpg"), rec(DL + "\\ORGANIZED\\images\\b.jpg"), rec(DL + "\\c.jpg")])
    assert skips(result) == {ROOT + "\\Images\\a.jpg": "already_in_place", DL + "\\ORGANIZED\\images\\b.jpg": "already_in_place"}
    assert list(moves(result)) == [DL + "\\c.jpg"]
    posix = plan([rec("/d/organized/images/b.jpg")], options(root="/d/Organized"), style=POSIX)
    assert moves(posix) == {"/d/organized/images/b.jpg": "/d/Organized/Images/b.jpg"}   # different folder on a case-sensitive system


def test_a_file_already_in_a_different_category_folder_is_moved_to_the_right_one():
    result = plan([rec(ROOT + "\\Documents\\photo.jpg")])
    assert moves(result) == {ROOT + "\\Documents\\photo.jpg": ROOT + "\\Images\\photo.jpg"}


def test_two_files_wanting_the_same_destination_are_both_left_alone():
    items = [rec(DL + "\\a\\report.pdf"), rec(DL + "\\b\\report.pdf"), rec(DL + "\\c\\other.pdf")]
    result = plan(items)
    assert skips(result) == {DL + "\\a\\report.pdf": "destination.collision", DL + "\\b\\report.pdf": "destination.collision"}
    assert list(moves(result)) == [DL + "\\c\\other.pdf"]
    reason = {s["path"]: s["reason"] for s in result["skipped"]}[DL + "\\a\\report.pdf"]
    assert DL + "\\b\\report.pdf" in reason and "none of them is moved" in reason


def test_windows_treats_names_that_differ_only_in_case_as_the_same_destination():
    items = [rec(DL + "\\a\\Report.pdf"), rec(DL + "\\b\\report.PDF")]
    assert set(skips(plan(items)).values()) == {"destination.collision"}
    assert len(plan([rec("/d/a/Report.pdf"), rec("/d/b/report.pdf")], options(root="/d/Organized"), style=POSIX)["plan"]["moves"]) == 2


def test_three_way_collisions_list_the_others():
    items = [rec(f"{DL}\\{n}\\x.jpg") for n in "abc"]
    reasons = {s["path"]: s["reason"] for s in plan(items)["skipped"]}
    assert len(reasons) == 3 and "2 other file(s)" in reasons[DL + "\\a\\x.jpg"]
    many = {s["path"]: s["reason"] for s in plan([rec(f"{DL}\\{n}\\x.jpg") for n in "abcdefg"])["skipped"]}
    assert "and 3 more" in many[DL + "\\a\\x.jpg"]


def test_an_existing_destination_is_never_overwritten():
    taken = {ROOT + "\\Images\\a.jpg"}
    result = plan([rec(DL + "\\a.jpg"), rec(DL + "\\b.jpg")], exists=lambda p: p in taken)
    assert skips(result) == {DL + "\\a.jpg": "destination.exists"} and list(moves(result)) == [DL + "\\b.jpg"]


def test_a_file_in_the_way_of_a_folder_blocks_only_that_folder():
    state = lambda p: "blocked" if p.endswith("\\Images") else "dir"  # noqa: E731
    result = plan([rec(DL + "\\a.jpg"), rec(DL + "\\b.pdf")], folder_state=state)
    assert skips(result) == {DL + "\\a.jpg": "destination.folder_blocked"} and list(moves(result)) == [DL + "\\b.pdf"]


def test_folders_that_would_be_created_are_listed_once_each():
    state = lambda p: "dir" if p.endswith("\\Documents") else "missing"  # noqa: E731
    result = plan([rec(DL + "\\a.jpg"), rec(DL + "\\b.jpg"), rec(DL + "\\c.pdf")], folder_state=state)
    assert result["plan"]["folders_to_create"] == [ROOT + "\\Images"] and result["stats"]["folders_to_create"] == 1


def test_unusable_paths_are_reported_not_fatal():
    items = [rec("relative\\a.jpg"), rec("C:\\"), rec(DL + "\\fine.jpg")]
    result = plan(items)
    assert skips(result) == {"relative\\a.jpg": "source.invalid", "C:\\": "source.invalid"}
    assert list(moves(result)) == [DL + "\\fine.jpg"]


# ---- the Rulebook decides ---------------------------------------------------------------------
def test_the_rulebook_is_asked_about_every_source_and_every_destination():
    asked = []

    def check(paths):
        asked.extend(paths)
        return allow_all(paths)

    plan([rec(DL + "\\a.jpg"), rec(DL + "\\b.pdf")], check=check)
    assert asked == [DL + "\\a.jpg", ROOT + "\\Images\\a.jpg", DL + "\\b.pdf", ROOT + "\\Documents\\b.pdf"]


def test_refusals_say_which_end_was_refused_and_why():
    def check(paths):
        out = []
        for p in paths:
            if p.endswith("secret.jpg"):
                out.append(PathVerdict(p, False, "path.forbidden", "inside a forbidden folder"))
            elif "\\Images\\" in p and p.endswith("b.jpg"):
                out.append(PathVerdict(p, False, "path.outside_allowed", "not inside any approved folder"))
            else:
                out.append(PathVerdict(p, True, "ok", "Allowed."))
        return out

    result = plan([rec(DL + "\\secret.jpg"), rec(DL + "\\b.jpg"), rec(DL + "\\c.jpg")], check=check)
    assert skips(result) == {DL + "\\secret.jpg": "path.forbidden", DL + "\\b.jpg": "path.outside_allowed"}
    reasons = {s["path"]: s["reason"] for s in result["skipped"]}
    assert reasons[DL + "\\secret.jpg"].startswith("Not allowed to move this file")
    assert reasons[DL + "\\b.jpg"].startswith("Not allowed to move it there")
    assert list(moves(result)) == [DL + "\\c.jpg"]


def test_a_rulebook_check_that_answers_the_wrong_number_of_questions_stops_the_plan():
    with pytest.raises(PlanConfigError) as caught:
        plan([rec(DL + "\\a.jpg")], check=lambda paths: allow_all(paths[:1]))
    assert caught.value.code == "plan.preflight_failed"


def test_a_refused_file_does_not_cause_a_collision_for_the_file_that_may_move():
    def check(paths):
        return [PathVerdict(p, "\\a\\" not in p, "path.forbidden", "no") for p in paths]

    result = plan([rec(DL + "\\a\\x.jpg"), rec(DL + "\\b\\x.jpg")], check=check)
    assert skips(result) == {DL + "\\a\\x.jpg": "path.forbidden"} and list(moves(result)) == [DL + "\\b\\x.jpg"]


# ---- the plan itself --------------------------------------------------------------------------
def test_the_same_input_gives_the_same_plan_in_any_order():
    items = [rec(f"{DL}\\{d}\\{n}.{e}", size=i, modified=f"2026-0{1 + i % 9}-0{1 + i % 9}T00:00:00Z")
             for i, (d, n, e) in enumerate((d, n, e) for d in "abc" for n in ("x", "y", "Z") for e in ("jpg", "pdf", "bin"))]
    reference = plan(items, options(unmatched_folder=("Misc",)))
    for seed in range(5):
        shuffled = items[:]
        random.Random(seed).shuffle(shuffled)
        again = plan(shuffled, options(unmatched_folder=("Misc",)))
        assert again == reference
    assert len(reference["plan"]["plan_id"]) == 64


def test_the_plan_id_changes_when_anything_that_matters_changes():
    base = plan([rec(DL + "\\a.jpg", 10)])["plan"]
    assert base["plan_id"] == plan([rec(DL + "\\a.jpg", 10)])["plan"]["plan_id"]
    assert base["plan_id"] != plan([rec(DL + "\\a.jpg", 11)])["plan"]["plan_id"]                    # different size
    assert base["plan_id"] != plan([rec(DL + "\\a.jpg", 10, modified="2026-03-05T00:00:00Z")])["plan"]["plan_id"]
    assert base["plan_id"] != plan([rec(DL + "\\a.jpg", 10, hash="ab" * 32)])["plan"]["plan_id"]    # now carries a hash
    assert base["plan_id"] != plan([rec(DL + "\\a.jpg", 10)], options(root=DL + "\\Sorted"))["plan"]["plan_id"]
    assert plan_fingerprint("r", []) == plan_fingerprint("r", [])


def test_hashes_are_carried_through_only_when_the_record_has_one():
    result = plan([rec(DL + "\\a.jpg", hash="ab" * 32, hash_algorithm="sha256"), rec(DL + "\\b.jpg"),
                   rec(DL + "\\c.jpg", hash=None, hash_algorithm=None)])
    by_name = {m["name"]: m for m in result["plan"]["moves"]}
    assert by_name["a.jpg"]["hash"] == "ab" * 32 and by_name["a.jpg"]["hash_algorithm"] == "sha256"
    assert "hash" not in by_name["b.jpg"]                      # never hashed stays "never hashed"
    assert by_name["c.jpg"]["hash"] is None


def test_stats_add_up():
    items = [rec(DL + "\\a.jpg", 100), rec(DL + "\\b.jpg", 50), rec(DL + "\\c.pdf", 7), rec(DL + "\\d.xyz"), rec(DL + "\\e.xyz")]
    stats = plan(items)["stats"]
    assert stats == {"files_considered": 5, "moves": 3, "skipped": 2, "bytes_to_move": 157, "folders_to_create": 2,
                     "by_category": {"Documents": 1, "Images": 2}, "skipped_by_reason": {"rule.no_match": 2}}


def test_the_plan_changes_nothing_in_its_input():
    items = [rec(DL + "\\a.jpg"), rec(DL + "\\b.pdf")]
    snapshot = [dict(i) for i in items]
    plan(items)
    assert items == snapshot


def test_an_empty_input_is_an_empty_plan_and_never_asks_the_rulebook():
    def check(paths):
        raise AssertionError("nothing to ask about")

    result = plan([], check=check)
    assert result["plan"]["moves"] == [] and result["stats"]["moves"] == 0 and result["skipped"] == []


def test_cancellation_stops_the_work():
    stop = StopFlag()
    stop.set()
    with pytest.raises(Stopped):
        plan([rec(DL + "\\a.jpg")], stop=stop)


# ---- settings that cannot work ----------------------------------------------------------------
@pytest.mark.parametrize("root", ["relative\\dir", "", "C:rel", "\\\\.\\C:", "C:\\x\\CON"])
def test_an_unusable_destination_stops_the_plan_with_a_clear_code(root):
    with pytest.raises(PlanConfigError) as caught:
        plan([rec(DL + "\\a.jpg")], options(root=root))
    assert caught.value.code == "plan.bad_destination"


def test_a_rule_folder_that_windows_would_reject_is_a_settings_error_not_a_pile_of_skips():
    with pytest.raises(PlanConfigError) as caught:
        plan([rec(DL + "\\a.jpg")], options(rules=[Rule("x", ("PROGRA~1",), {"extensions": [".jpg"]})]))
    assert caught.value.code == "plan.bad_folder" and "x" in caught.value.message


def test_an_unknown_date_mode_is_refused():
    with pytest.raises(PlanConfigError):
        plan([rec(DL + "\\a.jpg")], options(date_folders="weekly"))


# ---- folder names -----------------------------------------------------------------------------
@pytest.mark.parametrize("text,parts", [("Images", ("Images",)), ("Work/Invoices", ("Work", "Invoices")),
                                        ("Work\\2026\\Q1", ("Work", "2026", "Q1")), ("  Spaced name ", ("Spaced name",)),
                                        ("Trailing/", ("Trailing",)), ("a//b", ("a", "b")), ("Café ☕", ("Café ☕",))])
def test_good_folder_names(text, parts):
    assert parse_folder(text) == parts


@pytest.mark.parametrize("text", ["", "   ", "/abs", "\\abs", "C:\\abs", "C:rel", "..", "a/../b", ".", "a/./b", "bad<name",
                                  "what?", "star*", "pipe|", 'quo"te', "colon:stream", "dot.", "space ./x", "NUL", "con.txt",
                                  "COM1", "LPT9", "x" * 101, "/".join("d" * 3 for _ in range(9)), "tab\there"])
def test_bad_folder_names_are_refused_with_a_reason(text):
    with pytest.raises(ValueError) as caught:
        parse_folder(text)
    assert str(caught.value)


def test_dot_folders_get_a_specific_message():
    for text in ("..", ".", "a/../b"):
        with pytest.raises(ValueError, match="not allowed in folder names"):
            parse_folder(text)
