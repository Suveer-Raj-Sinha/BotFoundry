import hashlib
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from botfoundry.files import hasher
from botfoundry.files.duplicates import NotHashed, find_duplicates
from botfoundry.files.filtering import normalize_extension, run_filter
from botfoundry.files.fsutil import StopFlag, Stopped, to_extended_path
from botfoundry.files.hasher import hash_file, hash_items
from botfoundry.files.items import check_items, iso_utc
from botfoundry.files.scanner import RootUnreadable, ScanOptions, TooManyFiles, scan_folder

posix_only = pytest.mark.skipif(os.name == "nt", reason="uses POSIX symlinks and permissions")
windows_only = pytest.mark.skipif(os.name != "nt", reason="needs Windows")
not_root = pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root ignores file permissions")


def write(path: Path, content=b"x", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def scan(root: Path, allow_dir=lambda p: None, **options):
    return scan_folder(str(root), ScanOptions(**options), allow_dir=allow_dir, stop=StopFlag())


def names(result):
    return [os.path.relpath(i["path"], start=result["_root"]) for i in result["items"]]


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path.resolve() / "root"
    write(root / "b.TXT", "bb")
    write(root / "a.txt", "a")
    write(root / "sub" / "c.jpg", "ccc")
    write(root / "sub" / "deeper" / "d.png", "dddd")
    write(root / ".hidden" / "secret.txt", "s")
    write(root / ".dotfile", "x")
    (root / "empty").mkdir()
    return root


# ---- scanner ------------------------------------------------------------------------------
def test_scan_lists_files_with_complete_records(tree):
    result = scan(tree)
    rel = [os.path.relpath(i["path"], tree) for i in result["items"]]
    assert rel == ["a.txt", "b.TXT", os.path.join("sub", "c.jpg"), os.path.join("sub", "deeper", "d.png")]
    b = next(i for i in result["items"] if i["name"] == "b.TXT")
    assert (b["stem"], b["extension"], b["size"]) == ("b", ".txt", 2)                      # extension is lower-cased
    assert "hash" not in b                                                                # added only by files.hash
    assert b["modified"].endswith("Z") and datetime.fromisoformat(b["modified"].replace("Z", "+00:00")).tzinfo
    stats = result["stats"]
    assert stats["files"] == 4 and stats["bytes"] == 1 + 2 + 3 + 4 and stats["folders"] == 4    # root, sub, deeper, empty
    assert stats["hidden_skipped"] == 2 and result["skipped"] == []                              # .hidden and .dotfile


def test_scan_order_is_deterministic(tree):
    assert [i["path"] for i in scan(tree)["items"]] == [i["path"] for i in scan(tree)["items"]]


@pytest.mark.parametrize("options,expected", [
    ({"recursive": False}, ["a.txt", "b.TXT"]),
    ({"max_depth": 0}, ["a.txt", "b.TXT"]),
    ({"max_depth": 1}, ["a.txt", "b.TXT", "sub/c.jpg"]),
    ({"include_hidden": True}, [".dotfile", ".hidden/secret.txt", "a.txt", "b.TXT", "sub/c.jpg", "sub/deeper/d.png"]),
])
def test_scan_options(tree, options, expected):
    got = sorted(os.path.relpath(i["path"], tree).replace(os.sep, "/") for i in scan(tree, **options)["items"])
    assert got == sorted(expected)


def test_a_forbidden_subfolder_is_skipped_whole_and_reported(tree):
    forbidden = str(tree / "sub")
    result = scan(tree, allow_dir=lambda p: "forbidden by the Rulebook" if p == forbidden else None)
    assert [i["name"] for i in result["items"]] == ["a.txt", "b.TXT"]
    assert result["skipped"] == [{"path": forbidden, "reason": "forbidden by the Rulebook"}]


def test_special_characters_and_long_names(tmp_path):
    root = tmp_path.resolve() / "r"
    write(root / "naïve café — 写真.txt", "u")
    write(root / ("n" * 200 + ".dat"), "l")
    write(root / "with space  and.dots..txt", "d")
    assert sorted(i["name"] for i in scan(root)["items"]) == sorted(
        ["naïve café — 写真.txt", "n" * 200 + ".dat", "with space  and.dots..txt"])


def test_empty_folder_and_missing_folder(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert scan(empty)["items"] == []
    with pytest.raises(RootUnreadable) as info:
        scan(tmp_path / "nope")
    assert isinstance(info.value.cause, FileNotFoundError)
    write(tmp_path / "file.txt")
    with pytest.raises(RootUnreadable):
        scan(tmp_path / "file.txt")                                   # a file is not a folder


def test_too_many_files_is_an_error_not_a_silent_cut(tree):
    with pytest.raises(TooManyFiles) as info:
        scan(tree, max_items=3)
    assert info.value.limit == 3


def test_scan_can_be_stopped(tree):
    stopped = StopFlag()
    stopped.set()
    with pytest.raises(Stopped):
        scan_folder(str(tree), ScanOptions(), allow_dir=lambda p: None, stop=stopped)


@posix_only
def test_links_are_never_followed(tmp_path):
    outside = tmp_path / "outside"
    write(outside / "secret.txt", "classified")
    root = tmp_path / "root"
    write(root / "real.txt", "r")
    (root / "dirlink").symlink_to(outside)
    (root / "filelink.txt").symlink_to(outside / "secret.txt")
    result = scan(root)
    assert [i["name"] for i in result["items"]] == ["real.txt"]
    assert {os.path.basename(s["path"]): s["reason"] for s in result["skipped"]} == {
        "dirlink": "link or reparse point (not followed)", "filelink.txt": "link or reparse point (not followed)"}


@posix_only
@not_root
def test_an_unreadable_subfolder_is_reported_and_the_scan_continues(tree):
    locked = tree / "sub"
    locked.chmod(0)
    try:
        result = scan(tree)
    finally:
        locked.chmod(0o755)
    assert [i["name"] for i in result["items"]] == ["a.txt", "b.TXT"]
    assert result["skipped"][0]["path"] == str(locked) and "unreadable folder" in result["skipped"][0]["reason"]


@posix_only
@not_root
def test_an_unreadable_root_is_an_error(tmp_path):
    root = tmp_path / "locked"
    root.mkdir()
    root.chmod(0)
    try:
        with pytest.raises(RootUnreadable) as info:
            scan(root)
    finally:
        root.chmod(0o755)
    assert isinstance(info.value.cause, PermissionError)


def test_extended_path_prefix_rules():
    long = "C:\\" + "\\".join(["x" * 30] * 9)
    assert to_extended_path(long, windows=True) == "\\\\?\\" + long
    assert to_extended_path("\\\\srv\\share\\" + "y" * 250, windows=True).startswith("\\\\?\\UNC\\srv\\share\\")
    assert to_extended_path("C:\\short", windows=True) == "C:\\short"
    assert to_extended_path("\\\\?\\" + long, windows=True) == "\\\\?\\" + long          # already prefixed
    assert to_extended_path(long, windows=False) == long                                  # other systems untouched


@windows_only
def test_windows_long_paths_hidden_files_and_junctions(tmp_path):
    root = tmp_path.resolve() / "root"
    deep = root
    for _ in range(12):
        deep = deep / ("segment_" + "d" * 25)
    os.makedirs(to_extended_path(str(deep)))
    with open(to_extended_path(str(deep / "found.txt")), "wb") as handle:
        handle.write(b"deep")
    assert len(str(deep)) > 260
    write(root / "visible.txt", "v")
    hidden = write(root / "hidden.txt", "h")
    subprocess.run(["attrib", "+h", str(hidden)], check=True, capture_output=True)
    outside = tmp_path / "outside"
    write(outside / "secret.txt", "s")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "jn"), str(outside)], capture_output=True)
    assert made.returncode == 0, made.stderr
    result = scan(root)
    assert sorted(i["name"] for i in result["items"]) == ["found.txt", "visible.txt"]      # long path found, hidden skipped
    assert result["stats"]["hidden_skipped"] == 1
    assert [os.path.basename(s["path"]) for s in result["skipped"]] == ["jn"]               # the junction was not followed
    assert len(scan(root, include_hidden=True)["items"]) == 3


# ---- hashing ------------------------------------------------------------------------------
def test_known_hash_values_and_chunking(tmp_path):
    stop = StopFlag()
    assert hash_file(str(write(tmp_path / "abc", b"abc")), "sha256", stop) == \
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert hash_file(str(write(tmp_path / "empty", b"")), "sha256", stop) == hashlib.sha256(b"").hexdigest()
    blob = os.urandom(3 * 1024 * 1024 + 17)                         # spans several read chunks
    assert hash_file(str(write(tmp_path / "blob", blob)), "sha256", stop) == hashlib.sha256(blob).hexdigest()


def records(paths):
    return [{"path": str(p), "name": p.name, "stem": p.stem, "extension": p.suffix, "size": p.stat().st_size,
             "modified": iso_utc(p.stat().st_mtime)} for p in paths]


def test_hash_items_adds_hashes_without_changing_anything_else(tmp_path):
    files = [write(tmp_path / "a", "same"), write(tmp_path / "b", "same"), write(tmp_path / "c", "different")]
    items = records(files)
    out = hash_items(items, [i["path"] for i in items], "sha256", skip_unique_sizes=False, stop=StopFlag())
    assert [i["hash"] for i in out["items"][:2]] == [hashlib.sha256(b"same").hexdigest()] * 2
    assert all(i["hash_algorithm"] == "sha256" for i in out["items"])
    assert {k: v for k, v in out["items"][0].items() if k not in ("hash", "hash_algorithm")} == items[0]
    assert out["stats"] == {"hashed": 3, "not_needed": 0, "failed": 0, "bytes_read": 4 + 4 + 9}
    assert "hash" not in items[0]                                     # the input list was not modified


def test_unique_sizes_are_not_read(tmp_path):
    files = [write(tmp_path / "a", "1234"), write(tmp_path / "b", "abcd"), write(tmp_path / "c", "much longer")]
    items = records(files)
    out = hash_items(items, [i["path"] for i in items], "sha256", skip_unique_sizes=True, stop=StopFlag())
    by_name = {i["name"]: i for i in out["items"]}
    assert by_name["a"]["hash"] and by_name["b"]["hash"] and by_name["c"]["hash"] is None
    assert by_name["c"]["hash_algorithm"] is None and out["stats"]["not_needed"] == 1


def test_unreadable_files_are_reported_not_guessed(tmp_path):
    here = write(tmp_path / "here", "data")
    items = records([here, write(tmp_path / "gone", "data")])
    (tmp_path / "gone").unlink()
    out = hash_items(items, [i["path"] for i in items], "sha256", skip_unique_sizes=False, stop=StopFlag())
    assert [i["name"] for i in out["items"]] == ["here"]
    assert out["failed"] == [{"path": str(tmp_path / "gone"), "reason": "vanished"}]
    assert out["stats"]["failed"] == 1


@posix_only
@not_root
def test_permission_denied_is_reported(tmp_path):
    locked = write(tmp_path / "locked", "data")
    items = records([locked])
    locked.chmod(0)
    try:
        out = hash_items(items, [items[0]["path"]], "sha256", skip_unique_sizes=False, stop=StopFlag())
    finally:
        locked.chmod(0o644)
    assert out["failed"] == [{"path": str(locked), "reason": "permission denied"}] and out["items"] == []


def test_a_file_that_changed_since_the_scan_is_not_hashed(tmp_path):
    f = write(tmp_path / "f", "short")
    items = records([f])
    write(f, "now it is longer")
    out = hash_items(items, [items[0]["path"]], "sha256", skip_unique_sizes=False, stop=StopFlag())
    assert out["items"] == [] and "changed since it was scanned" in out["failed"][0]["reason"]


def test_a_file_that_changes_while_being_read_is_not_trusted(tmp_path, monkeypatch):
    f = write(tmp_path / "f", "stable")
    real_stat, calls = os.stat, []

    def shifting_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        calls.append(1)
        return SimpleNamespace(st_size=info.st_size, st_mtime_ns=info.st_mtime_ns + (len(calls) > 1) * 1_000)

    monkeypatch.setattr(hasher, "os", SimpleNamespace(stat=shifting_stat))
    with pytest.raises(ValueError, match="changed while it was being read"):
        hash_file(str(f), "sha256", StopFlag(), 6)


def test_hashing_stops_when_asked(tmp_path):
    big = write(tmp_path / "big", os.urandom(8 * 1024 * 1024))
    checks = []
    stop = StopFlag(lambda: len(checks.append(1) or checks) > 2)       # trips on the third check
    with pytest.raises(Stopped):
        hash_file(str(big), "sha256", stop)
    assert len(checks) == 3                                           # it did not keep reading to the end


# ---- records ------------------------------------------------------------------------------
def test_check_items_rejects_things_that_are_not_file_records():
    good = [{"path": "/a", "name": "a", "size": 1}]
    assert check_items(good, "x") is good
    for bad in ("nope", [1], [{"path": "/a", "name": "a"}], [{"path": "/a", "name": "a", "size": "1"}],
                [{"path": "/a", "name": "a", "size": True}], [{"path": 5, "name": "a", "size": 1}]):
        with pytest.raises(ValueError):
            check_items(bad, "x")


# ---- filtering ----------------------------------------------------------------------------
def item(name, size=10, modified="2026-01-01T00:00:00Z", **extra):
    stem, ext = os.path.splitext(name)
    return {"path": "/d/" + name, "name": name, "stem": stem, "extension": ext.lower(), "size": size,
            "modified": modified, "hash": None, **extra}


NOW = datetime(2026, 1, 31, tzinfo=timezone.utc)
FILES = [item("photo.JPG", 5_000), item("doc.pdf", 200), item("notes.txt", 20, "2025-06-01T00:00:00Z"),
         item("Report Final.PDF", 9_000, "2025-12-25T00:00:00Z"), item("README", 1)]


def split(criteria, mode="all", negate=False):
    matched, rejected = run_filter(FILES, criteria, mode, negate, NOW)
    return [i["name"] for i in matched], [i["name"] for i in rejected]


def test_filter_by_extension_name_and_size():
    assert split({"extensions": [".pdf"]})[0] == ["doc.pdf", "Report Final.PDF"]          # case-insensitive
    assert split({"extensions": [normalize_extension("jpg"), normalize_extension(".TXT")]})[0] == ["photo.JPG", "notes.txt"]
    assert split({"name_contains": ["REPORT", "note"]})[0] == ["notes.txt", "Report Final.PDF"]
    assert split({"min_size": 1_000})[0] == ["photo.JPG", "Report Final.PDF"]
    assert split({"max_size": 20})[0] == ["notes.txt", "README"]
    assert split({"extensions": [""]})[0] == ["README"]                                    # files with no extension


def test_filter_by_age_uses_the_reference_time():
    assert split({"older_than_days": 90})[0] == ["notes.txt"]
    assert split({"newer_than_days": 35})[0] == ["photo.JPG", "doc.pdf", "README"]          # 30 days old; the report is 37
    assert split({"newer_than_days": 10})[0] == []
    assert split({"older_than_days": 90})[1] == ["photo.JPG", "doc.pdf", "Report Final.PDF", "README"]
    undated = [item("x.txt", modified=None)]
    assert run_filter(undated, {"older_than_days": 1}, "all", False, NOW)[0] == []         # unknown age never matches


def test_filter_all_any_and_negate():
    criteria = {"extensions": [".pdf"], "min_size": 1_000}
    assert split(criteria, "all")[0] == ["Report Final.PDF"]
    assert split(criteria, "any")[0] == ["photo.JPG", "doc.pdf", "Report Final.PDF"]
    assert split({"extensions": [".pdf"]}, negate=True)[0] == ["photo.JPG", "notes.txt", "README"]


def test_filter_never_loses_or_duplicates_items():
    matched, rejected = run_filter(FILES, {"min_size": 100}, "all", False, NOW)
    assert sorted(i["name"] for i in matched + rejected) == sorted(i["name"] for i in FILES)


# ---- duplicates ---------------------------------------------------------------------------
def hashed(name, digest, size=10, modified="2026-01-01T00:00:00Z", path=None):
    return {**item(name, size, modified), "path": path or "/d/" + name, "hash": digest}


def test_duplicates_are_grouped_by_content():
    items = [hashed("a", "H1"), hashed("b", "H1"), hashed("c", "H2"), hashed("d", "H1", path="/d/sub/d"),
             hashed("e", "H3", size=5), hashed("f", "H3", size=5), hashed("g", None)]
    out = find_duplicates(items)
    assert [(g["hash"], g["count"], g["wasted_bytes"]) for g in out["groups"]] == [("H1", 3, 20), ("H3", 2, 5)]
    assert out["stats"] == {"groups": 2, "duplicate_files": 3, "wasted_bytes": 25, "files_examined": 7}
    group = out["groups"][0]
    assert group["keep"]["name"] not in [d["name"] for d in group["duplicates"]]
    assert sorted([group["keep"]["name"]] + [d["name"] for d in group["duplicates"]]) == ["a", "b", "d"]
    assert len(out["duplicates"]) == 3 and out["groups"][0]["keep"] not in out["duplicates"]


def test_same_hash_but_different_size_is_not_a_duplicate():
    out = find_duplicates([hashed("a", "H", size=1), hashed("b", "H", size=2)])
    assert out["groups"] == []


@pytest.mark.parametrize("strategy,expected", [
    ("oldest", "old"), ("newest", "new"), ("shortest_path", "s"), ("first", "zzz")])
def test_which_copy_to_keep(strategy, expected):
    items = [hashed("zzz", "H", modified="2026-03-01T00:00:00Z", path="/a/long/path/zzz"),
             hashed("old", "H", modified="2020-01-01T00:00:00Z", path="/a/long/path/old"),
             hashed("new", "H", modified="2026-12-01T00:00:00Z", path="/a/long/path/new"),
             hashed("s", "H", modified="2026-06-01T00:00:00Z", path="/s")]
    assert find_duplicates(items, keep=strategy)["groups"][0]["keep"]["name"] == expected


def test_ties_are_broken_predictably():
    same = "2026-01-01T00:00:00Z"
    items = [hashed("b", "H", modified=same, path="/x/yy/b"), hashed("a", "H", modified=same, path="/x/a")]
    assert find_duplicates(items, keep="oldest")["groups"][0]["keep"]["path"] == "/x/a"        # shorter path wins the tie
    assert find_duplicates(list(reversed(items)))["groups"][0]["keep"]["path"] == "/x/a"        # input order does not matter


def test_empty_files_are_ignored_unless_asked():
    items = [hashed("a", "E", size=0), hashed("b", "E", size=0)]
    assert find_duplicates(items)["groups"] == []
    assert find_duplicates(items, ignore_empty=False)["groups"][0]["wasted_bytes"] == 0


def test_unhashed_input_is_refused_and_inputs_are_untouched():
    with pytest.raises(NotHashed):
        find_duplicates([{"path": "/a", "name": "a", "size": 1}])           # no 'hash' field at all
    items = [hashed("a", "H"), hashed("b", "H")]
    before = [dict(i) for i in items]
    find_duplicates(items)
    assert items == before
    with pytest.raises(ValueError):
        find_duplicates(items, keep="random")
