from __future__ import annotations

import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from spg.config import Command, ProjectConfig
from spg.registry import Registry

WRAPPER_MARKER = "# spg-managed:"


class InstallError(Exception):
    """Raised when a wrapper cannot be written safely."""


@dataclass(frozen=True)
class WrapperMeta:
    project: str
    command: str


@dataclass
class InstallResult:
    project: str
    written: list[str]
    removed: list[str]
    refreshed: list[str]


def install_project(
    config: ProjectConfig,
    registry: Registry,
    bin_dir: Path,
    *,
    force: bool = False,
) -> InstallResult:
    """Materialize wrappers for `config` into `bin_dir` and update the registry.

    Detects conflicts against:
      - existing non-spg files in bin_dir (error unless force=True)
      - spg wrappers owned by a different registered project (always an error)
      - symlinks at the wrapper path (error unless force=True; even with force,
        the symlink dirent is atomically replaced rather than followed)

    Removes any wrappers from a prior install of this project that no longer appear in config.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    with registry.locked():
        return _install_project_locked(config, registry, bin_dir, force=force)


def _install_project_locked(
    config: ProjectConfig,
    registry: Registry,
    bin_dir: Path,
    *,
    force: bool,
) -> InstallResult:
    new_command_names = tuple(c.name for c in config.commands)
    previous_entry = registry.projects.get(config.name)

    if previous_entry is not None:
        prev_root = previous_entry.root
        if not prev_root.is_absolute():
            prev_root = prev_root.resolve()
        this_root = config.root.resolve()
        if prev_root != this_root:
            raise InstallError(
                f"project {config.name!r} is already installed from {prev_root}; "
                f"refusing to take it over from {this_root}. "
                f"Uninstall the existing one (`spg uninstall {config.name}`) or rename "
                f"this project's [project].name in {this_root}/spg.toml."
            )

    previous_commands: tuple[str, ...] = previous_entry.commands if previous_entry else ()

    _check_conflicts(config, registry, bin_dir, force=force)

    # Commands that still want a ~/bin wrapper after this install.
    wrapper_command_names = {c.name for c in config.commands if not c.is_shell_function}

    written: list[str] = []
    refreshed: list[str] = []
    for cmd in config.commands:
        if cmd.is_shell_function:
            continue
        wrapper_path = bin_dir / cmd.name
        already_existed = _lstat_or_none(wrapper_path) is not None
        _write_wrapper(wrapper_path, project=config.name, command=cmd, root=config.root)
        if already_existed:
            refreshed.append(cmd.name)
        else:
            written.append(cmd.name)

    removed: list[str] = []
    # Remove any wrappers we previously owned that no longer want one — either
    # because the command was deleted, or because it switched to shell_function.
    for orphan in previous_commands:
        if orphan in wrapper_command_names:
            continue
        wrapper_path = bin_dir / orphan
        meta = _read_wrapper_meta(wrapper_path)
        if meta is not None and meta.project == config.name:
            wrapper_path.unlink()
            removed.append(orphan)

    registry.upsert(config.name, config.root, new_command_names)
    registry.save()

    return InstallResult(
        project=config.name,
        written=written,
        removed=removed,
        refreshed=refreshed,
    )


@dataclass
class UninstallResult:
    project: str
    removed: list[str]
    skipped: list[str]


def uninstall_project(
    project_name: str,
    registry: Registry,
    bin_dir: Path,
) -> UninstallResult:
    with registry.locked():
        entry = registry.projects.get(project_name)
        if entry is None:
            raise InstallError(f"Project {project_name!r} is not installed")

        removed: list[str] = []
        skipped: list[str] = []
        for cmd_name in entry.commands:
            wrapper_path = bin_dir / cmd_name
            if _lstat_or_none(wrapper_path) is None:
                # No file in ~/bin — expected for shell_function commands.
                continue
            meta = _read_wrapper_meta(wrapper_path)
            if meta is None:
                skipped.append(cmd_name)
                continue
            if meta.project != project_name:
                skipped.append(cmd_name)
                continue
            wrapper_path.unlink()
            removed.append(cmd_name)

        registry.remove(project_name)
        registry.save()
        return UninstallResult(project=project_name, removed=removed, skipped=skipped)


def _check_conflicts(
    config: ProjectConfig,
    registry: Registry,
    bin_dir: Path,
    *,
    force: bool,
) -> None:
    errors: list[str] = []
    for cmd in config.commands:
        if cmd.is_shell_function:
            owner = registry.find_owner_of_command(cmd.name)
            if owner is not None and owner.name != config.name:
                errors.append(
                    f"command {cmd.name!r} is already registered to project {owner.name!r}"
                )
            continue
        wrapper_path = bin_dir / cmd.name
        st = _lstat_or_none(wrapper_path)
        if st is None:
            owner = registry.find_owner_of_command(cmd.name)
            if owner is not None and owner.name != config.name:
                errors.append(
                    f"command {cmd.name!r} is registered to project {owner.name!r} "
                    f"(but no wrapper exists at {wrapper_path}; run `spg sync`)"
                )
            continue
        if stat.S_ISLNK(st.st_mode):
            if not force:
                errors.append(
                    f"{wrapper_path} is a symlink; refusing to overwrite. "
                    "Remove it manually, or pass --force to replace it with a managed wrapper."
                )
            continue
        if not stat.S_ISREG(st.st_mode):
            errors.append(
                f"{wrapper_path} exists and is not a regular file; refusing to overwrite."
            )
            continue
        meta = _read_wrapper_meta(wrapper_path)
        if meta is None:
            if not force:
                errors.append(
                    f"{wrapper_path} exists and is not managed by spg; pass --force to overwrite"
                )
            continue
        if meta.project != config.name:
            errors.append(
                f"command {cmd.name!r} is already provided by project {meta.project!r} "
                f"({wrapper_path}). Rename it in spg.toml or uninstall the other project."
            )
    if errors:
        raise InstallError("\n  - ".join(["install would fail:", *errors]))


def _write_wrapper(path: Path, *, project: str, command: Command, root: Path) -> None:
    """Atomically (re)create `path` as a regular executable wrapper.

    Uses tempfile + atomic replace so we never open the existing dirent for writing;
    if `path` was a symlink, rename(2) replaces the dirent rather than following it.
    """
    content = _render_wrapper(project=project, command=command, root=root)
    directory = path.parent
    fd, tmp_path_str = tempfile.mkstemp(prefix=".spg-wrapper-", dir=str(directory))
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        tmp_path.chmod(0o755)
        tmp_path.replace(path)
    except Exception:
        with suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def _render_wrapper(*, project: str, command: Command, root: Path) -> str:
    quoted_root = _sh_single_quote(str(root))
    inner = f'{command.run} "$@"'
    quoted_inner = _sh_single_quote(inner)
    description = command.description.strip() or "(no description)"
    return (
        "#!/bin/sh\n"
        f"{WRAPPER_MARKER} {project}:{command.name}\n"
        f"# {description}\n"
        "# Do not edit — regenerated by `spg install` / `spg sync`.\n"
        f"cd {quoted_root} || exit 1\n"
        f'exec /bin/sh -c {quoted_inner} spg "$@"\n'
    )


def _sh_single_quote(value: str) -> str:
    """Quote a string for safe embedding in single quotes in a POSIX shell."""
    return "'" + value.replace("'", "'\\''") + "'"


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _read_wrapper_meta(path: Path) -> WrapperMeta | None:
    st = _lstat_or_none(path)
    if st is None or not stat.S_ISREG(st.st_mode):
        return None
    try:
        with path.open("r") as f:
            for _ in range(10):
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped.startswith(WRAPPER_MARKER):
                    continue
                payload = stripped[len(WRAPPER_MARKER) :].strip()
                if ":" not in payload:
                    return None
                project, command = payload.split(":", 1)
                project = project.strip()
                command = command.strip()
                if not project or not command:
                    return None
                return WrapperMeta(project=project, command=command)
    except OSError:
        return None
    return None


def sync_project(
    config: ProjectConfig,
    registry: Registry,
    bin_dir: Path,
) -> InstallResult:
    """Refresh wrappers for an already-registered project."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    with registry.locked():
        if config.name not in registry.projects:
            raise InstallError(f"Project {config.name!r} is not installed; run `spg install` first")
        return _install_project_locked(config, registry, bin_dir, force=False)


def list_managed_wrappers(bin_dir: Path) -> list[tuple[Path, WrapperMeta]]:
    if not bin_dir.is_dir():
        return []
    out: list[tuple[Path, WrapperMeta]] = []
    for child in sorted(bin_dir.iterdir()):
        meta = _read_wrapper_meta(child)
        if meta is not None:
            out.append((child, meta))
    return out


__all__ = [
    "InstallError",
    "InstallResult",
    "UninstallResult",
    "WrapperMeta",
    "install_project",
    "list_managed_wrappers",
    "sync_project",
    "uninstall_project",
]
