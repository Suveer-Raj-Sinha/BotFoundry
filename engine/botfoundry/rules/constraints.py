"""System safety constraints: limits no Rulebook can override (they rank above it)."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .paths import POSIX, WINDOWS, native_style
from .permissions import PERMISSIONS, Policy


@dataclass(frozen=True)
class SystemConstraints:
    protected_paths: tuple[str, ...] = ()  # bots may never touch these, nor anything inside them
    too_broad_paths: tuple[str, ...] = ()  # an approved folder may not BE one of these (or a drive root)
    ceilings: Mapping[str, Policy] = field(default_factory=lambda: {n: p.system_ceiling for n, p in PERMISSIONS.items()})
    style: object = None  # path style; None = the host's own

    def path_style(self):
        return self.style or native_style()


def _env(*names: str) -> list[str]:
    return [os.environ[n] for n in names if os.environ.get(n)]


def default_constraints(data_dir: Path | str | None = None) -> SystemConstraints:
    """Protected locations for this machine, plus BotFoundry's own data folder (its database must never be
    something a bot can read or change)."""
    style = native_style()
    if style is WINDOWS:
        protected = _env("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData",
                         "APPDATA", "LOCALAPPDATA")
        too_broad = _env("USERPROFILE", "PUBLIC")
    else:
        protected = ["/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/dev", "/proc", "/sys", "/var", "/root"]
        too_broad = [os.path.expanduser("~")]
    if data_dir:
        protected.append(str(Path(data_dir).resolve()))
    return SystemConstraints(protected_paths=tuple(protected), too_broad_paths=tuple(too_broad), style=style)
