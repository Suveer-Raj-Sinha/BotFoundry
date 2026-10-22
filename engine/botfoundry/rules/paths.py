"""Path authorization: may the engine touch this path?

A path is judged in two stages:
  1. Lexically: normalize it (Windows rules: case-insensitive, `/` and `\\`, `..`, trailing dots and
     spaces, `\\\\?\\` prefixes, reserved device names, alternate data streams, 8.3 short names) and check
     it lies inside an approved folder and outside forbidden and protected ones.
  2. Physically (native platform only): follow symlinks and junctions to the real location and check
     again, so a link inside an approved folder cannot lead outside it.

IMPORTANT: this is a check at one moment. Code that then acts on the path must act on `resolved`, and
should re-check immediately before a destructive step; the filesystem can change in between."""
import ntpath
import os
import posixpath
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from pydantic import BaseModel


class PathError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class NPath:
    """A normalized absolute path. `key_*` are what comparisons use (case-folded on Windows)."""
    root: str
    parts: tuple[str, ...]
    key_root: str
    key_parts: tuple[str, ...]
    sep: str

    def __str__(self) -> str:
        return self.root + self.sep.join(self.parts)

    @property
    def is_root(self) -> bool:
        return not self.parts


def contains(root: NPath, child: NPath) -> bool:
    """Is `child` the same as, or inside, `root`? Compares whole components (so `C:\\Allowed2` is NOT
    inside `C:\\Allowed`)."""
    return (root.key_root == child.key_root
            and child.key_parts[:len(root.key_parts)] == root.key_parts)


_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_RESERVED |= {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_RESERVED |= {f"{p}{d}" for p in ("COM", "LPT") for d in "\u00b9\u00b2\u00b3"}
_SHORT_NAME = re.compile(r"^[^~]{1,8}~\d+(\.[^.]{0,3})?$")
_DRIVE = re.compile(r"^[A-Za-z]:$")
_BAD_CHARS = set('<>"|?*')


def _fold(text: str) -> str:
    """Case folding like NTFS: simple per-character upper-casing ('ß' stays 'ß', it does not become 'SS')."""
    return "".join(c.upper() if len(c.upper()) == 1 else c for c in text)


class WindowsStyle:
    name = "windows"

    @property
    def native(self) -> bool:
        return os.name == "nt"

    def normalize(self, raw: str) -> NPath:
        if not isinstance(raw, str) or not raw.strip():
            raise PathError("path.invalid", "The path is empty.")
        if any(ord(c) < 32 for c in raw):
            raise PathError("path.invalid", "The path contains control characters.")
        p = raw.replace("/", "\\")
        if p.startswith("\\\\.\\"):
            raise PathError("path.device_namespace", "Device paths (\\\\.\\) are not allowed.")
        if p.startswith("\\\\?\\"):
            rest = p[4:]
            if rest[:4].upper() == "UNC\\":
                p = "\\\\" + rest[4:]
            elif len(rest) >= 2 and rest[1] == ":":
                p = rest
            else:
                raise PathError("path.device_namespace", "This kind of extended path (volume GUID, GLOBALROOT, ...) is not allowed.")
        drive, rest = ntpath.splitdrive(p)
        if not drive:
            raise PathError("path.not_absolute", "The path must be absolute (start with a drive letter such as C:\\ or a \\\\server\\share).")
        if not drive.startswith("\\\\") and not _DRIVE.match(drive):
            raise PathError("path.invalid", f"Unrecognized drive '{drive}'.")
        if drive.startswith("\\\\"):
            names = drive[2:].split("\\")
            if len(names) < 2 or not names[0] or not names[1]:
                raise PathError("path.not_absolute", "A network path needs a server and a share name (\\\\server\\share).")
        if drive.startswith("\\\\") and rest == "":
            rest = "\\"  # a bare \\server\share is the root of that share
        if not rest.startswith("\\"):
            raise PathError("path.not_absolute", "A drive-relative path (like C:folder) is not allowed; use C:\\folder.")
        if ":" in rest:
            raise PathError("path.stream", "Alternate data streams (a colon after the file name) are not allowed.")

        parts: list[str] = []
        for segment in rest.split("\\"):
            if segment in ("", "."):
                continue
            if segment == "..":
                if parts:
                    parts.pop()  # Windows clamps at the root
                continue
            if any(c in _BAD_CHARS for c in segment):
                raise PathError("path.invalid", f"'{segment}' contains characters that are not allowed ({''.join(sorted(_BAD_CHARS))}).")
            name = segment.rstrip(" .")  # Windows ignores trailing dots and spaces
            if not name:
                raise PathError("path.invalid", f"'{segment}' is not a valid name.")
            if name.split(".")[0].rstrip(" ").upper() in _RESERVED:
                raise PathError("path.reserved_name", f"'{name}' is a reserved Windows device name.")
            if _SHORT_NAME.match(name):
                raise PathError("path.short_name", f"'{name}' looks like a Windows 8.3 short name; use the full folder name.")
            parts.append(name)

        shown_drive = drive.upper() if _DRIVE.match(drive) else drive
        return NPath(root=shown_drive + "\\", parts=tuple(parts), key_root=_fold(drive) + "\\",
                     key_parts=tuple(_fold(p) for p in parts), sep="\\")


class PosixStyle:
    name = "posix"

    @property
    def native(self) -> bool:
        return os.name != "nt"

    def normalize(self, raw: str) -> NPath:
        if not isinstance(raw, str) or not raw.strip():
            raise PathError("path.invalid", "The path is empty.")
        if "\x00" in raw:
            raise PathError("path.invalid", "The path contains a NUL character.")
        if not raw.startswith("/"):
            raise PathError("path.not_absolute", "The path must be absolute (start with /).")
        parts: list[str] = []
        for segment in raw.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(segment)
        return NPath(root="/", parts=tuple(parts), key_root="/", key_parts=tuple(parts), sep="/")


WINDOWS = WindowsStyle()
POSIX = PosixStyle()


def native_style() -> "WindowsStyle | PosixStyle":
    return WINDOWS if os.name == "nt" else POSIX


def resolve_real(path: str) -> str:
    """The real location of a path: symlinks and junctions followed for the part that exists, and the
    not-yet-existing remainder (e.g. a destination about to be created) appended unchanged."""
    current, tail = path, []
    while not os.path.lexists(current):
        parent, name = os.path.split(current)
        if parent == current or not name:
            break
        tail.append(name)
        current = parent
    real = os.path.realpath(current)
    for name in reversed(tail):
        real = os.path.join(real, name)
    return real


class PathDecision(BaseModel):
    path: str
    allowed: bool
    code: str  # "ok" or "path.<reason>"
    message: str
    resolved: str | None = None  # use THIS path to act, not the original text
    root: str | None = None  # the approved folder that contains it


def _roots(raw_paths: Iterable[str], style: "WindowsStyle | PosixStyle") -> list[NPath]:
    roots = []
    for raw in raw_paths:
        try:
            roots.append(style.normalize(raw))
        except PathError:
            continue  # rulebook validation reports unusable folders; they simply never match
    return roots


def authorize_path(raw: str, *, allowed: Sequence[str], forbidden: Sequence[str] = (),
                   protected: Sequence[str] = (), style: "WindowsStyle | PosixStyle | None" = None,
                   resolve: bool = True) -> PathDecision:
    style = style or native_style()

    def deny(code: str, message: str, resolved: str | None = None) -> PathDecision:
        return PathDecision(path=raw, allowed=False, code=code, message=message, resolved=resolved)

    try:
        n = style.normalize(raw)
    except PathError as err:
        return deny(err.code, err.message)

    allowed_roots = _roots(allowed, style)
    match = next((r for r in allowed_roots if contains(r, n)), None)
    if match is None:
        return deny("path.outside_allowed", f"'{n}' is not inside any approved folder.")
    if any(contains(f, n) for f in _roots(forbidden, style)):
        return deny("path.forbidden", f"'{n}' is inside a folder this bot is forbidden to use.")
    if any(contains(p, n) for p in _roots(protected, style)):
        return deny("path.protected", f"'{n}' is inside a protected system location.")

    if not (resolve and style.native):
        return PathDecision(path=raw, allowed=True, code="ok", message="Allowed.", resolved=str(n), root=str(match))

    # Physical check: follow links, then require the REAL location to be inside the REAL approved folder.
    # POSIX resolves ".." physically (after following links), so it must see the raw path; on Windows ".." is
    # purely textual, so the normalized path is the right thing to resolve.
    real_text = resolve_real(raw if style is POSIX else str(n))
    try:
        real = style.normalize(real_text)
    except PathError as err:
        return deny(err.code, f"'{n}' resolves to '{real_text}', which is not usable: {err.message}")

    def real_roots(raw_paths: Sequence[str]) -> list[NPath]:
        out = []
        for root in _roots(raw_paths, style):
            try:
                out.append(style.normalize(resolve_real(str(root))))
            except PathError:
                continue
        return out

    if not any(contains(r, real) for r in real_roots(allowed)):
        return deny("path.escapes_via_link",
                    f"'{n}' leads (through a link or junction) to '{real}', outside the approved folders.", str(real))
    if any(contains(f, real) for f in real_roots(forbidden)):
        return deny("path.forbidden", f"'{n}' leads to '{real}', inside a forbidden folder.", str(real))
    if any(contains(p, real) for p in real_roots(protected)):
        return deny("path.protected", f"'{n}' leads to '{real}', inside a protected system location.", str(real))
    return PathDecision(path=raw, allowed=True, code="ok", message="Allowed.", resolved=str(real), root=str(match))
