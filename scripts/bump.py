#!/usr/bin/env python3
"""Keep BotFoundry versions and changelogs in sync.

  python scripts/bump.py engine 0.3.0   set the engine version (+ add a changelog stub if missing)
  python scripts/bump.py app 0.4.0      set the app version in all three files (+ stub)
  python scripts/bump.py check          verify versions match each other and the changelogs
"""
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_INIT = ROOT / "engine" / "botfoundry" / "__init__.py"
ENGINE_LOG = ROOT / "engine" / "CHANGELOG.md"
APP_DIR = ROOT / "apps" / "desktop"
APP_PACKAGE = APP_DIR / "package.json"
APP_CARGO = APP_DIR / "src-tauri" / "Cargo.toml"
APP_CONF = APP_DIR / "src-tauri" / "tauri.conf.json"
APP_LOG = APP_DIR / "CHANGELOG.md"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\][^\n]*\n(.*?)(?=^## \[|\Z)", re.M | re.S)

# (file, pattern capturing the version) -- first match only
JSON_VERSION = re.compile(r'^(  "version": ")([^"]*)(")', re.M)
CARGO_VERSION = re.compile(r'^(version = ")([^"]*)(")', re.M)
INIT_VERSION = re.compile(r'^(__version__ = ")([^"]*)(")', re.M)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def get_version(path: Path, pattern: re.Pattern) -> str | None:
    m = pattern.search(read(path)) if path.exists() else None
    return m.group(2) if m else None


def set_version(path: Path, pattern: re.Pattern, version: str) -> None:
    text = read(path)
    new, count = pattern.subn(lambda m: m.group(1) + version + m.group(3), text, count=1)
    if count != 1:
        raise SystemExit(f"Could not find a version field in {path}")
    path.write_text(new, encoding="utf-8", newline="")


def changelog_top(path: Path) -> tuple[str | None, str]:
    m = HEADING.search(read(path)) if path.exists() else None
    return (m.group(1), m.group(2)) if m else (None, "")


def add_changelog_stub(path: Path, version: str) -> bool:
    text = read(path)
    if re.search(rf"^## \[{re.escape(version)}\]", text, re.M):
        return False
    stub = f"## [{version}] - {datetime.date.today().isoformat()}\n\n### Added\n- TODO: describe what changed\n\n"
    m = re.search(r"^## \[", text, re.M)
    text = text[: m.start()] + stub + text[m.start():] if m else text.rstrip() + "\n\n" + stub
    path.write_text(text, encoding="utf-8", newline="")
    return True


def check() -> int:
    problems: list[str] = []

    engine = get_version(ENGINE_INIT, INIT_VERSION)
    top, body = changelog_top(ENGINE_LOG)
    if engine != top:
        problems.append(f"engine: __version__ is {engine} but CHANGELOG top entry is {top}")
    if "TODO" in body:
        problems.append("engine: top CHANGELOG entry still contains a TODO")

    app_versions = {
        "package.json": get_version(APP_PACKAGE, JSON_VERSION),
        "Cargo.toml": get_version(APP_CARGO, CARGO_VERSION),
        "tauri.conf.json": get_version(APP_CONF, JSON_VERSION),
    }
    if len(set(app_versions.values())) != 1:
        problems.append(f"app: versions differ across files: {app_versions}")
    app = app_versions["package.json"]
    top, body = changelog_top(APP_LOG)
    if app != top:
        problems.append(f"app: version is {app} but CHANGELOG top entry is {top}")
    if "TODO" in body:
        problems.append("app: top CHANGELOG entry still contains a TODO")

    if problems:
        print("Version check FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"Version check OK  (engine {engine}, app {app})")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "check":
        return check()
    if len(argv) == 3 and argv[1] in ("engine", "app"):
        version = argv[2]
        if not SEMVER.match(version):
            print(f"'{version}' is not a version like 1.2.3")
            return 2
        if argv[1] == "engine":
            set_version(ENGINE_INIT, INIT_VERSION, version)
            stub = add_changelog_stub(ENGINE_LOG, version)
        else:
            set_version(APP_PACKAGE, JSON_VERSION, version)
            set_version(APP_CARGO, CARGO_VERSION, version)
            set_version(APP_CONF, JSON_VERSION, version)
            stub = add_changelog_stub(APP_LOG, version)
        print(f"{argv[1]} version set to {version}" + (" (changelog stub added)" if stub else ""))
        return check()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
