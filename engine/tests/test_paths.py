import os
import subprocess
import sys

import pytest

from botfoundry.rules.paths import POSIX, WINDOWS, PathError, authorize_path, contains, resolve_real

W = dict(style=WINDOWS, resolve=False)      # lexical Windows rules; runs identically on any host OS


def norm(raw):
    return str(WINDOWS.normalize(raw))


# ---- Windows normalization ---------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("C:\\Users\\Me\\Downloads", "C:\\Users\\Me\\Downloads"),
    ("c:/users/me/downloads/", "C:\\users\\me\\downloads"),                      # slashes, drive case, trailing sep
    ("C:\\Users\\\\Me\\.\\Downloads", "C:\\Users\\Me\\Downloads"),               # empty and dot segments
    ("C:\\Users\\Me\\..\\You\\Downloads", "C:\\Users\\You\\Downloads"),          # lexical ..
    ("C:\\..\\..\\x", "C:\\x"),                                                   # Windows clamps at the root
    ("C:\\Users\\Me.\\Downloads ", "C:\\Users\\Me\\Downloads"),                  # trailing dots/spaces are ignored by Windows
    ("\\\\?\\C:\\Users\\Me", "C:\\Users\\Me"),                                    # extended-length prefix
    ("\\\\?\\UNC\\srv\\share\\dir", "\\\\srv\\share\\dir"),
    ("\\\\srv\\share\\dir\\..\\other", "\\\\srv\\share\\other"),
    ("C:\\" + "\\".join(["segment"] * 60), "C:\\" + "\\".join(["segment"] * 60)),  # long paths are fine
    ("C:\\", "C:\\"),
])
def test_windows_normalization(raw, expected):
    assert norm(raw) == expected


@pytest.mark.parametrize("raw,code", [
    ("", "path.invalid"), ("   ", "path.invalid"), ("C:\\a\x00b", "path.invalid"), ("C:\\a\nb", "path.invalid"),
    ("relative\\dir", "path.not_absolute"), ("\\rooted", "path.not_absolute"), ("C:folder", "path.not_absolute"),
    ("C:", "path.not_absolute"), ("\\\\server", "path.not_absolute"),
    ("C:\\a\\NUL", "path.reserved_name"), ("C:\\a\\nul.txt", "path.reserved_name"), ("C:\\CON", "path.reserved_name"),
    ("C:\\a\\COM1", "path.reserved_name"), ("C:\\a\\lpt9.log", "path.reserved_name"),
    ("C:\\a\\COM\u00b9", "path.reserved_name"), ("C:\\a\\AUX ", "path.reserved_name"),
    ("C:\\a\\file.txt:stream", "path.stream"), ("C:\\a:b", "path.stream"),
    ("C:\\a\\*.txt", "path.invalid"), ("C:\\a\\what?", "path.invalid"), ("C:\\a\\x<y", "path.invalid"),
    ("C:\\a\\...\\b", "path.invalid"), ("C:\\a\\ \\b", "path.invalid"),
    ("\\\\.\\COM1", "path.device_namespace"), ("\\\\?\\GLOBALROOT\\Device\\x", "path.device_namespace"),
    ("\\\\?\\Volume{1234}\\x", "path.device_namespace"),
    ("C:\\PROGRA~1\\x", "path.short_name"), ("C:\\Users\\LONGNA~2", "path.short_name"),
])
def test_windows_rejections(raw, code):
    with pytest.raises(PathError) as info:
        WINDOWS.normalize(raw)
    assert info.value.code == code


def test_comparison_is_case_insensitive_like_ntfs():
    a, b = WINDOWS.normalize("c:\\ALLOWED\\Sub"), WINDOWS.normalize("C:\\allowed\\sub\\File.TXT")
    assert contains(a, b) and not contains(b, a)
    assert str(b) == "C:\\allowed\\sub\\File.TXT"                  # original case is kept for display
    assert not contains(WINDOWS.normalize("C:\\straße"), WINDOWS.normalize("C:\\STRASSE"))   # NTFS does not fold ß to SS


def test_containment_is_by_whole_component():
    root = WINDOWS.normalize("C:\\Allowed")
    assert contains(root, root)
    assert not contains(root, WINDOWS.normalize("C:\\Allowed2\\x"))        # the classic prefix bug
    assert not contains(root, WINDOWS.normalize("D:\\Allowed\\x"))         # other drive
    unc = WINDOWS.normalize("\\\\srv\\share\\dir")
    assert contains(unc, WINDOWS.normalize("\\\\SRV\\Share\\dir\\f"))
    assert not contains(unc, WINDOWS.normalize("\\\\srv\\other\\dir\\f"))


# ---- authorize_path, lexical -------------------------------------------------------------
ALLOWED = ["C:\\Users\\Me\\Downloads"]


def test_inside_the_approved_folder_is_allowed():
    d = authorize_path("c:/users/me/DOWNLOADS/new/file.txt", allowed=ALLOWED, **W)
    assert d.allowed and d.code == "ok" and d.root == "C:\\Users\\Me\\Downloads"
    assert d.resolved == "C:\\users\\me\\DOWNLOADS\\new\\file.txt"


@pytest.mark.parametrize("raw", [
    "C:\\Users\\Me\\Documents", "C:\\Users\\Me\\Downloads2\\x", "C:\\Users\\Me\\Downloads\\..\\secrets",
    "C:\\Users\\Me\\Downloads\\..\\..\\Me2", "D:\\Users\\Me\\Downloads", "C:\\",
])
def test_outside_the_approved_folder_is_denied(raw):
    d = authorize_path(raw, allowed=ALLOWED, **W)
    assert not d.allowed and d.code == "path.outside_allowed"


def test_invalid_paths_are_denied_with_their_own_code():
    assert authorize_path("C:\\Users\\Me\\Downloads\\NUL", allowed=ALLOWED, **W).code == "path.reserved_name"
    assert authorize_path("C:\\Users\\Me\\Downloads\\a.txt:evil", allowed=ALLOWED, **W).code == "path.stream"
    assert authorize_path("Downloads", allowed=ALLOWED, **W).code == "path.not_absolute"


def test_forbidden_subfolders_win_over_allowed():
    kwargs = dict(allowed=ALLOWED, forbidden=["C:\\Users\\Me\\Downloads\\Private"], **W)
    assert authorize_path("C:\\Users\\Me\\Downloads\\ok.txt", **kwargs).allowed
    d = authorize_path("c:\\users\\me\\downloads\\PRIVATE\\a.txt", **kwargs)
    assert not d.allowed and d.code == "path.forbidden"


def test_protected_locations_win_over_allowed():
    d = authorize_path("C:\\Windows\\System32\\x.dll", allowed=["C:\\Windows"], protected=["C:\\Windows"], **W)
    assert not d.allowed and d.code == "path.protected"


def test_trailing_dot_and_space_tricks_do_not_escape():
    d = authorize_path("C:\\Users\\Me\\Downloads. \\..\\Secrets", allowed=ALLOWED, **W)
    assert not d.allowed
    d = authorize_path("C:\\Users\\Me\\Downloads\\sub. \\file", allowed=ALLOWED, **W)
    assert d.allowed and d.resolved == "C:\\Users\\Me\\Downloads\\sub\\file"


def test_unusable_approved_folders_never_match():
    d = authorize_path("C:\\x", allowed=["not absolute", "C:\\NUL"], **W)
    assert d.code == "path.outside_allowed"


def test_extended_prefix_does_not_bypass_checks():
    assert authorize_path("\\\\?\\C:\\Users\\Me\\Downloads\\a.txt", allowed=ALLOWED, **W).allowed
    assert not authorize_path("\\\\?\\C:\\Users\\Me\\Documents\\a.txt", allowed=ALLOWED, **W).allowed


# ---- POSIX style (development hosts) ------------------------------------------------------
def test_posix_normalization():
    assert str(POSIX.normalize("/home/me//data/./x/../y")) == "/home/me/data/y"
    for bad in ("relative/path", "", "/a\x00b"):
        with pytest.raises(PathError):
            POSIX.normalize(bad)
    assert not contains(POSIX.normalize("/home/Me"), POSIX.normalize("/home/me/x"))     # case-sensitive


# ---- real filesystem: symlinks (POSIX hosts) ----------------------------------------------
posix_only = pytest.mark.skipif(os.name == "nt", reason="uses POSIX symlinks")


@posix_only
def test_symlink_leading_outside_is_denied(tmp_path):
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir(); outside.mkdir()
    (outside / "secret.txt").write_text("x")
    (allowed / "link").symlink_to(outside)
    kwargs = dict(allowed=[str(allowed)], style=POSIX)
    d = authorize_path(str(allowed / "link" / "secret.txt"), **kwargs)
    assert not d.allowed and d.code == "path.escapes_via_link" and d.resolved == str(outside / "secret.txt")
    assert not authorize_path(str(allowed / "link" / "new" / "dest.txt"), **kwargs).allowed   # not-yet-existing destination


@posix_only
def test_symlink_that_stays_inside_is_allowed_and_resolved(tmp_path):
    allowed = tmp_path / "allowed"
    (allowed / "real").mkdir(parents=True)
    (allowed / "alias").symlink_to(allowed / "real")
    d = authorize_path(str(allowed / "alias" / "file.txt"), allowed=[str(allowed)], style=POSIX)
    assert d.allowed and d.resolved == str(allowed / "real" / "file.txt")


@posix_only
def test_dotdot_after_a_symlink_is_judged_by_where_it_really_goes(tmp_path):
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (allowed / "plain").mkdir(parents=True)
    (allowed / "link").symlink_to(outside / "deep")
    # Textually "allowed/link/../x" collapses to "allowed/x", but the kernel follows the link first and
    # then steps up, landing in "outside/x". The physical check must catch that.
    d = authorize_path(str(allowed) + "/link/../x", allowed=[str(allowed)], style=POSIX)
    assert not d.allowed and d.code == "path.escapes_via_link" and d.resolved == str(outside / "x")
    # A ".." through an ordinary folder is harmless and resolves where it looks.
    d = authorize_path(str(allowed) + "/plain/../x", allowed=[str(allowed)], style=POSIX)
    assert d.allowed and d.resolved == str(allowed / "x")


@posix_only
def test_approved_folder_that_is_itself_a_link_still_works(tmp_path):
    real, link = tmp_path / "real", tmp_path / "link"
    real.mkdir(); link.symlink_to(real)
    d = authorize_path(str(link / "a.txt"), allowed=[str(link)], style=POSIX)
    assert d.allowed and d.resolved == str(real / "a.txt")


@posix_only
def test_forbidden_folder_reached_through_a_link_is_denied(tmp_path):
    allowed = tmp_path / "allowed"
    (allowed / "private").mkdir(parents=True)
    (allowed / "shortcut").symlink_to(allowed / "private")
    d = authorize_path(str(allowed / "shortcut" / "f"), allowed=[str(allowed)],
                       forbidden=[str(allowed / "private")], style=POSIX)
    assert not d.allowed and d.code == "path.forbidden"


@posix_only
def test_resolve_real_handles_missing_tails(tmp_path):
    (tmp_path / "a").mkdir()
    assert resolve_real(str(tmp_path / "a" / "x" / "y")) == os.path.join(os.path.realpath(tmp_path / "a"), "x", "y")


# ---- real filesystem: Windows junctions and symlinks (run on a Windows machine) ------------
windows_only = pytest.mark.skipif(os.name != "nt", reason="needs Windows")


@windows_only
def test_windows_junction_leading_outside_is_denied(tmp_path):
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir(); outside.mkdir()
    (outside / "secret.txt").write_text("x")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(allowed / "jn"), str(outside)], capture_output=True)
    assert made.returncode == 0, made.stderr
    d = authorize_path(str(allowed / "jn" / "secret.txt"), allowed=[str(allowed)])
    assert not d.allowed and d.code == "path.escapes_via_link"
    assert authorize_path(str(allowed / "plain.txt"), allowed=[str(allowed)]).allowed


@windows_only
def test_windows_case_and_slashes_on_a_real_folder(tmp_path):
    folder = tmp_path / "Allowed"
    folder.mkdir()
    shouted = str(folder).replace("\\", "/").upper()
    assert authorize_path(shouted + "/File.TXT", allowed=[str(folder)]).allowed


@windows_only
def test_windows_symlink_leading_outside_is_denied(tmp_path):
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir(); outside.mkdir()
    try:
        (allowed / "sym").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or administrator rights")
    assert not authorize_path(str(allowed / "sym" / "x.txt"), allowed=[str(allowed)]).allowed
