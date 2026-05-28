from __future__ import annotations

import os
from pathlib import Path

PROJECT_CONFIG_FILENAME = "spg.toml"


def home() -> Path:
    return Path(os.path.expanduser("~"))


def bin_dir() -> Path:
    return home() / "bin"


def registry_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else home() / ".config"
    return base / "spg" / "registry.toml"


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
