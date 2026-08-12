from __future__ import annotations

import os
from pathlib import Path

PROJECT_CONFIG_FILENAME = "spg.toml"


def home() -> Path:
    return Path("~").expanduser()


def bin_dir() -> Path:
    return home() / "bin"


def registry_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else home() / ".config"
    return base / "spg" / "registry.toml"


def invocation_dir() -> Path:
    """Return the directory from which spg was invoked, or CWD if not set."""
    env = os.environ.get("SPG_INVOCATION_DIR")
    if env:
        return Path(env)
    return Path.cwd()


def resolve_path(path: str | Path) -> Path:
    """Resolve a path string or Path relative to invocation_dir()."""
    p = Path(path)
    if not p.is_absolute():
        p = invocation_dir() / p
    return p.resolve()


def find_project_config(start: Path) -> Path | None:
    """Walk upward from `start` looking for spg.toml. Returns None if not found."""
    current = start.resolve()
    while True:
        candidate = current / PROJECT_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent
