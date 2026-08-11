from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Collection
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from spg.config import Command, Link, ProjectConfig, display_selector
from spg.registry import Registry, RegistryEntry, RegistryLink

WRAPPER_MARKER = "# spg-managed:"

# State of the filesystem where a declared link wants to live.
#   missing    — nothing there; safe to create
#   current    — a symlink already pointing exactly at the declared source
#   equivalent — a symlink resolving to the declared source by another path
#   foreign    — a symlink pointing somewhere else
#   file/dir   — a regular file / real directory sits in the way
#   other      — something else entirely (fifo, socket, …)
LinkState = Literal["missing", "current", "equivalent", "foreign", "file", "dir", "other"]


class InstallError(Exception):
    """Raised when a wrapper cannot be written safely."""


@dataclass(frozen=True)
class WrapperMeta:
    project: str
    command: str


@dataclass(frozen=True)
class ExclusionChange:
    """A request to decline or re-accept declared items, by declaration name.

    One value type for every surface that can change what a user has declined:
    `spg install --without`, the interactive checklist, and
    `spg disable`/`spg enable`. Applied as (stored | disable) - enable, so a
    surface that knows the whole answer (the checklist) can express a
    replacement while a surface that knows one name can express a delta.
    """

    disable_commands: tuple[str, ...] = ()
    disable_links: tuple[str, ...] = ()
    enable_commands: tuple[str, ...] = ()
    enable_links: tuple[str, ...] = ()


NO_EXCLUSION_CHANGE = ExclusionChange()


@dataclass
class InstallResult:
    project: str
    written: list[str]
    removed: list[str]
    refreshed: list[str]
    links_written: list[str] = field(default_factory=list)
    links_relinked: list[str] = field(default_factory=list)
    links_removed: list[str] = field(default_factory=list)
    # Everything currently declined, as display selectors (`cmd:x`, `link:y`).
    # Reported on every install/sync so a declined item never looks like a bug.
    excluded: list[str] = field(default_factory=list)


def install_project(
    config: ProjectConfig,
    registry: Registry,
    bin_dir: Path,
    *,
    force: bool = False,
    changes: ExclusionChange = NO_EXCLUSION_CHANGE,
) -> InstallResult:
    """Materialize wrappers and links for `config` and update the registry.

    Detects conflicts against:
      - existing non-spg files in bin_dir (error unless force=True)
      - spg wrappers owned by a different registered project (always an error)
      - symlinks at the wrapper path (error unless force=True; even with force,
        the symlink dirent is atomically replaced rather than followed)
      - declared `[links]` whose path is taken by a foreign symlink or file
        (error unless force=True), by a real directory (always an error), or by
        a link another project already publishes (always an error)

    Removes any wrappers and links from a prior install of this project that no
    longer appear in config.

    `changes` declines (or re-accepts) individual declared items for this user;
    the resulting exclusions are stored on the registry entry and honored by
    every later install and sync.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    with registry.locked():
        return _install_project_locked(config, registry, bin_dir, force=force, changes=changes)


def _install_project_locked(
    config: ProjectConfig,
    registry: Registry,
    bin_dir: Path,
    *,
    force: bool,
    changes: ExclusionChange = NO_EXCLUSION_CHANGE,
) -> InstallResult:
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

    # Resolve what this user has declined *inside the lock*: `previous_entry`
    # was read after `locked()` refreshed state from disk, so this is the only
    # trustworthy view of the stored exclusions. Everything below works from
    # `effective` — the declared config minus the declined items — including
    # conflict detection, so a declined item cannot fail an install over a
    # collision it no longer causes.
    excluded_commands = _effective_exclusions(
        previous_entry.excluded_commands if previous_entry else (),
        changes.disable_commands,
        changes.enable_commands,
    )
    excluded_links = _effective_exclusions(
        previous_entry.excluded_links if previous_entry else (),
        changes.disable_links,
        changes.enable_links,
    )
    effective = config.without(commands=excluded_commands, links=excluded_links)
    new_command_names = tuple(c.name for c in effective.commands)

    _check_conflicts(effective, registry, bin_dir, force=force)

    # Commands that still want a ~/bin wrapper after this install.
    wrapper_command_names = {c.name for c in effective.commands if not c.is_shell_function}

    written: list[str] = []
    refreshed: list[str] = []
    for cmd in effective.commands:
        if cmd.is_shell_function:
            continue
        wrapper_path = bin_dir / cmd.name
        already_existed = _lstat_or_none(wrapper_path) is not None
        _write_wrapper(wrapper_path, project=config.name, command=cmd, root=config.root)
        if already_existed:
            refreshed.append(cmd.name)
        else:
            written.append(cmd.name)

    links_written: list[str] = []
    links_relinked: list[str] = []
    for link in effective.links:
        link_path = link.link_path
        source = link.source_path(config.root)
        state = _link_state(link_path, source)
        if state == "current":
            continue
        _write_symlink(link_path, source)
        if state == "missing":
            links_written.append(link.name)
        else:
            links_relinked.append(link.name)

    removed: list[str] = []
    # Remove any wrappers we previously owned that no longer want one — either
    # because the command was deleted, because it switched to shell_function, or
    # because the user just declined it.
    for orphan in previous_commands:
        if orphan in wrapper_command_names:
            continue
        wrapper_path = bin_dir / orphan
        meta = _read_wrapper_meta(wrapper_path)
        if meta is not None and meta.project == config.name:
            wrapper_path.unlink()
            removed.append(orphan)

    # Same for links this project used to publish but no longer wants — either
    # undeclared upstream or declined here.
    declared_link_paths = {link.link_path for link in effective.links}
    links_removed: list[str] = []
    for prev_link in previous_entry.links if previous_entry else ():
        if prev_link.path in declared_link_paths:
            continue
        if _remove_symlink(prev_link.path):
            links_removed.append(prev_link.name)

    registry.upsert(
        config.name,
        config.root,
        new_command_names,
        links=tuple(RegistryLink(name=link.name, path=link.link_path) for link in effective.links),
        excluded_commands=excluded_commands,
        excluded_links=excluded_links,
    )
    registry.save()

    return InstallResult(
        project=config.name,
        written=written,
        removed=removed,
        refreshed=refreshed,
        links_written=links_written,
        links_relinked=links_relinked,
        links_removed=links_removed,
        excluded=excluded_selectors(excluded_commands, excluded_links),
    )


def _effective_exclusions(
    stored: tuple[str, ...],
    disable: tuple[str, ...],
    enable: tuple[str, ...],
) -> tuple[str, ...]:
    """(stored | disable) - enable, sorted and deduped.

    Never pruned against what the project currently declares: honoring intent
    across an upstream spg.toml that drops and later restores an item matters
    more than tidiness, and erroring on a stored name would let one upstream
    edit break `spg sync` machine-wide.
    """
    return tuple(sorted((set(stored) | set(disable)) - set(enable)))


def excluded_selectors(
    excluded_commands: tuple[str, ...],
    excluded_links: tuple[str, ...],
) -> list[str]:
    """Declined items as sorted display selectors, for reporting."""
    return sorted(
        [display_selector("command", name) for name in excluded_commands]
        + [display_selector("link", name) for name in excluded_links]
    )


def entry_excluded_selectors(entry: RegistryEntry) -> list[str]:
    """Display selectors for everything a registry entry records as declined."""
    return excluded_selectors(entry.excluded_commands, entry.excluded_links)


@dataclass
class UninstallResult:
    project: str
    removed: list[str]
    skipped: list[str]
    links_removed: list[str] = field(default_factory=list)
    links_skipped: list[str] = field(default_factory=list)


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

        links_removed: list[str] = []
        links_skipped: list[str] = []
        for link in entry.links:
            st = _lstat_or_none(link.path)
            if st is None:
                continue
            if not stat.S_ISLNK(st.st_mode):
                # Something replaced our symlink with a real file or directory;
                # leave it alone rather than deleting data we don't own.
                links_skipped.append(link.name)
                continue
            link.path.unlink()
            links_removed.append(link.name)

        registry.remove(project_name)
        registry.save()
        return UninstallResult(
            project=project_name,
            removed=removed,
            skipped=skipped,
            links_removed=links_removed,
            links_skipped=links_skipped,
        )


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
    errors.extend(_link_conflicts(config, registry, bin_dir, force=force))
    if errors:
        raise InstallError("\n  - ".join(["install would fail:", *errors]))


def _link_conflicts(
    config: ProjectConfig,
    registry: Registry,
    bin_dir: Path,
    *,
    force: bool,
) -> list[str]:
    """Reasons the `[links]` in `config` cannot be materialized safely."""
    errors: list[str] = []
    previous_entry = registry.projects.get(config.name)
    planned_wrappers = {bin_dir / c.name for c in config.commands if not c.is_shell_function}

    for link in config.links:
        link_path = link.link_path
        source = link.source_path(config.root)
        if _lstat_or_none(source) is None:
            errors.append(
                f"link {link.name!r}: source {source} does not exist "
                f"(relative to the project root {config.root})"
            )
            continue
        if link_path in planned_wrappers:
            errors.append(
                f"link {link.name!r} would be created at {link_path}, which is also the wrapper "
                "path for a command in this project. Rename one of them."
            )
            continue
        owner = registry.find_owner_of_link(link_path)
        if owner is not None and owner.name != config.name:
            errors.append(f"{link_path} is already published by project {owner.name!r}")
            continue

        state = _link_state(link_path, source)
        if state in ("missing", "current", "equivalent"):
            continue
        if state == "foreign":
            was_ours = previous_entry is not None and previous_entry.link(link_path) is not None
            if was_ours or force:
                continue
            errors.append(
                f"{link_path} is an existing symlink to {_readlink_or_unknown(link_path)}; "
                "refusing to repoint it. Remove it manually, or pass --force."
            )
            continue
        if state == "dir":
            hint = ""
            if not link.target_is_dir:
                hint = (
                    f" If you meant to link into it, write "
                    f'[links.{link.name}].target = "{link.target}/" (trailing slash).'
                )
            errors.append(f"{link_path} is a directory; refusing to replace it.{hint}")
            continue
        if state == "file":
            meta = _read_wrapper_meta(link_path)
            if meta is not None:
                errors.append(
                    f"{link_path} is the spg wrapper for command {meta.command!r} of project "
                    f"{meta.project!r}; refusing to replace it with a link."
                )
            elif not force:
                errors.append(
                    f"{link_path} exists and is not managed by spg; pass --force to replace it"
                )
            continue
        errors.append(
            f"{link_path} exists and is neither a regular file nor a symlink; "
            "refusing to replace it."
        )
    return errors


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
        # Preserve the caller's directory before cd'ing into the project root,
        # so commands can act on where the user is rather than where the tool
        # is installed. Captured with `pwd` (not $PWD) so it's robust even when
        # PWD isn't exported into the wrapper's environment.
        'SPG_INVOCATION_DIR="$(pwd)"; export SPG_INVOCATION_DIR\n'
        f"cd {quoted_root} || exit 1\n"
        f'exec /bin/sh -c {quoted_inner} spg "$@"\n'
    )


def _write_symlink(link_path: Path, source: Path) -> None:
    """Atomically (re)create `link_path` as a symlink to `source`.

    Creates missing parent directories, then symlinks a private temp name and
    renames it into place, so an existing symlink's dirent is replaced rather
    than followed (and no half-written state is ever visible).
    """
    parent = link_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(prefix=".spg-link-", dir=str(parent))
    os.close(fd)
    tmp_path = Path(tmp_path_str)
    try:
        # mkstemp made a regular file; free the (private) name for symlink(2).
        tmp_path.unlink()
        tmp_path.symlink_to(source)
        tmp_path.replace(link_path)
    except Exception:
        with suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def _remove_symlink(path: Path) -> bool:
    """Unlink `path` if it is a symlink. Returns whether it was removed."""
    st = _lstat_or_none(path)
    if st is None or not stat.S_ISLNK(st.st_mode):
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _link_state(link_path: Path, source: Path) -> LinkState:
    st = _lstat_or_none(link_path)
    if st is None:
        return "missing"
    if stat.S_ISLNK(st.st_mode):
        current = _readlink_or_unknown(link_path)
        if current == str(source):
            return "current"
        if _points_at(link_path, current, source):
            return "equivalent"
        return "foreign"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "other"


def _points_at(link_path: Path, current: str, source: Path) -> bool:
    """Whether a symlink's existing value resolves to the same place as `source`.

    Catches hand-made links that name the source by a different but equivalent
    path, so we treat them as already-correct instead of a foreign conflict.
    """
    # A relative link value resolves against the directory holding the link.
    return (link_path.parent / current).resolve() == source.resolve()


def _readlink_or_unknown(path: Path) -> str:
    try:
        return str(path.readlink())
    except OSError:
        return "<unreadable>"


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
    *,
    changes: ExclusionChange = NO_EXCLUSION_CHANGE,
) -> InstallResult:
    """Refresh wrappers for an already-registered project.

    Also the entry point for `spg disable` / `spg enable`: it requires the
    project to be registered already and never forces, which is exactly the
    semantics those want.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    with registry.locked():
        if config.name not in registry.projects:
            raise InstallError(f"Project {config.name!r} is not installed; run `spg install` first")
        return _install_project_locked(config, registry, bin_dir, force=False, changes=changes)


@dataclass
class PruneResult:
    removed: list[str]


def prune_orphan_wrappers(
    registry: Registry,
    bin_dir: Path,
    *,
    skip_projects: Collection[str] = (),
) -> PruneResult:
    """Remove spg-managed wrappers that no registered command backs.

    A wrapper is orphaned when its marker names a project that is not in the
    registry, or a command absent from that project's registry entry — e.g.
    the project was unregistered without `spg uninstall`, or an interrupted
    install left a wrapper the registry never recorded. Projects listed in
    `skip_projects` (ones whose spg.toml could not be read during `spg sync`)
    are left untouched, as are files without an spg marker.
    """
    with registry.locked():
        removed: list[str] = []
        for path, meta in list_managed_wrappers(bin_dir):
            if meta.project in skip_projects:
                continue
            entry = registry.projects.get(meta.project)
            if entry is not None and meta.command in entry.commands:
                continue
            path.unlink()
            removed.append(path.name)
        return PruneResult(removed=removed)


def link_state(link: Link, root: Path) -> LinkState:
    """Public wrapper: what the filesystem holds where `link` wants to live."""
    return _link_state(link.link_path, link.source_path(root))


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
    "NO_EXCLUSION_CHANGE",
    "ExclusionChange",
    "InstallError",
    "InstallResult",
    "LinkState",
    "PruneResult",
    "UninstallResult",
    "WrapperMeta",
    "entry_excluded_selectors",
    "excluded_selectors",
    "install_project",
    "link_state",
    "list_managed_wrappers",
    "prune_orphan_wrappers",
    "sync_project",
    "uninstall_project",
]
