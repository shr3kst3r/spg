from __future__ import annotations

import fcntl
import os
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Format version of ~/.config/spg/registry.toml. Bumped only for changes this
# version of spg could not read correctly; purely additive keys don't bump it.
# Files written before versioning (no `version` key) are treated as version 1.
REGISTRY_VERSION = 1


class RegistryError(Exception):
    """Raised on registry I/O or consistency errors."""


@dataclass(frozen=True)
class RegistryLink:
    """A symlink spg created for a project. `path` is the link itself."""

    name: str
    path: Path


@dataclass
class RegistryEntry:
    name: str
    root: Path
    commands: tuple[str, ...]
    installed_at: str
    links: tuple[RegistryLink, ...] = ()

    def to_table(self) -> dict[str, Any]:
        table: dict[str, Any] = {
            "root": str(self.root),
            "commands": list(self.commands),
            "installed_at": self.installed_at,
        }
        if self.links:
            table["links"] = [{"name": link.name, "path": str(link.path)} for link in self.links]
        return table

    def link(self, path: Path) -> RegistryLink | None:
        for link in self.links:
            if link.path == path:
                return link
        return None


@dataclass
class Registry:
    path: Path
    projects: dict[str, RegistryEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Registry:
        if not path.exists():
            return cls(path=path)
        with path.open("rb") as f:
            raw = tomllib.load(f)

        version = raw.get("version", REGISTRY_VERSION)
        if not isinstance(version, int):
            raise RegistryError(f"{path}: version must be an integer")
        if version > REGISTRY_VERSION:
            raise RegistryError(
                f"{path}: registry format version {version} is newer than this spg understands "
                f"(version {REGISTRY_VERSION}); upgrade spg."
            )

        projects_raw = raw.get("projects", {})
        if not isinstance(projects_raw, dict):
            raise RegistryError(f"{path}: [projects] must be a table")

        projects: dict[str, RegistryEntry] = {}
        for name, body in projects_raw.items():
            if not isinstance(body, dict):
                raise RegistryError(f"{path}: [projects.{name}] must be a table")
            root = body.get("root")
            commands = body.get("commands", [])
            installed_at = body.get("installed_at", "")
            if not isinstance(root, str):
                raise RegistryError(f"{path}: projects.{name}.root must be a string")
            if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
                raise RegistryError(f"{path}: projects.{name}.commands must be a list of strings")
            if not isinstance(installed_at, str):
                raise RegistryError(f"{path}: projects.{name}.installed_at must be a string")
            projects[name] = RegistryEntry(
                name=name,
                root=Path(root),
                commands=tuple(commands),
                installed_at=installed_at,
                links=_parse_links(path, name, body.get("links", [])),
            )
        return cls(path=path, projects=projects)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = _format_registry(self.projects)
        _atomic_write_text(self.path, body)

    @contextmanager
    def locked(self) -> Iterator[Registry]:
        """Hold an exclusive file lock and refresh in-memory state from disk.

        Wraps the whole install/uninstall/sync transaction so concurrent spg
        invocations cannot interleave wrapper writes and registry rewrites.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.parent / (self.path.name + ".lock")
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    fresh = Registry.load(self.path)
                    self.projects = fresh.projects
                else:
                    self.projects = {}
                yield self
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def upsert(
        self,
        name: str,
        root: Path,
        commands: tuple[str, ...],
        links: tuple[RegistryLink, ...] = (),
    ) -> RegistryEntry:
        entry = RegistryEntry(
            name=name,
            root=root.resolve(),
            commands=commands,
            installed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            links=links,
        )
        self.projects[name] = entry
        return entry

    def remove(self, name: str) -> RegistryEntry | None:
        return self.projects.pop(name, None)

    def find_by_root(self, root: Path) -> RegistryEntry | None:
        target = root.resolve()
        for entry in self.projects.values():
            if entry.root == target:
                return entry
        return None

    def find_owner_of_command(self, command: str) -> RegistryEntry | None:
        for entry in self.projects.values():
            if command in entry.commands:
                return entry
        return None

    def find_owner_of_link(self, link_path: Path) -> RegistryEntry | None:
        for entry in self.projects.values():
            if entry.link(link_path) is not None:
                return entry
        return None

    def __iter__(self) -> Iterator[RegistryEntry]:
        return iter(self.projects.values())


def _parse_links(path: Path, name: str, raw: Any) -> tuple[RegistryLink, ...]:
    if not isinstance(raw, list):
        raise RegistryError(f"{path}: projects.{name}.links must be a list of tables")
    links: list[RegistryLink] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RegistryError(f"{path}: projects.{name}.links entries must be tables")
        link_name = item.get("name")
        link_path = item.get("path")
        if not isinstance(link_name, str) or not isinstance(link_path, str):
            raise RegistryError(
                f"{path}: projects.{name}.links entries need string 'name' and 'path'"
            )
        links.append(RegistryLink(name=link_name, path=Path(link_path)))
    return tuple(links)


def _format_registry(projects: dict[str, RegistryEntry]) -> str:
    lines: list[str] = [
        "# spg registry — managed file, edit with care\n",
        f"version = {REGISTRY_VERSION}\n",
        "\n",
    ]
    for name in sorted(projects):
        entry = projects[name]
        lines.append(f"[projects.{_toml_key(name)}]\n")
        lines.append(f"root = {_toml_str(str(entry.root))}\n")
        commands_inner = ", ".join(_toml_str(c) for c in entry.commands)
        lines.append(f"commands = [{commands_inner}]\n")
        if entry.links:
            links_inner = ", ".join(
                f"{{ name = {_toml_str(link.name)}, path = {_toml_str(str(link.path))} }}"
                for link in entry.links
            )
            lines.append(f"links = [{links_inner}]\n")
        lines.append(f"installed_at = {_toml_str(entry.installed_at)}\n")
        lines.append("\n")
    return "".join(lines)


def _toml_key(name: str) -> str:
    bare = all(ch.isalnum() or ch in "_-" for ch in name) and name != ""
    if bare:
        return name
    return _toml_str(name)


def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _atomic_write_text(path: Path, content: str) -> None:
    directory = path.parent
    fd, tmp_path = tempfile.mkstemp(prefix=".spg-registry-", dir=str(directory))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        Path(tmp_path).replace(path)
    except Exception:
        with suppress(FileNotFoundError):
            Path(tmp_path).unlink()
        raise
