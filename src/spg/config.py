from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Literal, cast

from spg.paths import PROJECT_CONFIG_FILENAME

_COMMAND_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
# Link names double as filenames when `target` names a directory, so leading
# dots and digits are allowed (`.zshrc`, `1password`) — but never '.' or '..'.
_LINK_NAME_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")


class ConfigError(Exception):
    """Raised when spg.toml is missing required fields or malformed."""


# Which namespace a selector names. Command, link, and install names live in
# separate tables and may collide, so a bare name is not always enough.
SelectorKind = Literal["command", "link", "install"]

# One declared item, by namespace and name. What `requires` resolves to.
Ref = tuple[SelectorKind, str]

_ARG_TYPES = ("files", "directories")


@dataclass(frozen=True)
class CommandArg:
    name: str
    description: str = ""
    type: str = ""
    values: tuple[str, ...] = ()

    @property
    def is_flag(self) -> bool:
        return self.name.startswith("-")

    @property
    def expects_value(self) -> bool:
        return bool(self.type or self.values) if self.is_flag else True


@dataclass(frozen=True)
class Command:
    name: str
    run: str = ""
    description: str = ""
    args: tuple[CommandArg, ...] = ()
    complete_hook: str = ""
    shell_function: str = ""
    requires: tuple[Ref, ...] = ()

    @property
    def is_shell_function(self) -> bool:
        return bool(self.shell_function)


@dataclass(frozen=True)
class Link:
    """A symlink this project publishes outside the repo.

    `source` is a path relative to the project root; `target` is where the
    symlink is created. A `target` ending in '/' names a *directory* to link
    into, and the link's leaf name is `name`; otherwise `target` is the exact
    path of the symlink.
    """

    name: str
    source: str
    target: str
    description: str = ""
    requires: tuple[Ref, ...] = ()

    @property
    def target_is_dir(self) -> bool:
        return self.target.endswith("/")

    @property
    def link_path(self) -> Path:
        expanded = Path(self.target).expanduser()
        return expanded / self.name if self.target_is_dir else expanded

    def source_path(self, root: Path) -> Path:
        return root / self.source


@dataclass(frozen=True)
class Install:
    """Software this project needs on the machine, installed by running `run`.

    Exactly one idempotency probe is set: `check` (a shell command; exit 0
    means already installed) or `creates` (a path; existing means already
    installed). The `brew`/`cask` shorthands are expanded into `run` and
    `check` at parse time and kept only for display.
    """

    name: str
    run: str
    check: str = ""
    creates: str = ""
    description: str = ""
    brew: tuple[str, ...] = ()
    cask: tuple[str, ...] = ()
    requires: tuple[Ref, ...] = ()

    @property
    def creates_path(self) -> Path | None:
        return Path(self.creates).expanduser() if self.creates else None


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    root: Path
    commands: tuple[Command, ...] = field(default_factory=tuple)
    links: tuple[Link, ...] = field(default_factory=tuple)
    installs: tuple[Install, ...] = field(default_factory=tuple)
    # Extra files pulled in by [project].include, in load order.
    included_files: tuple[Path, ...] = field(default_factory=tuple)

    def command(self, name: str) -> Command | None:
        for cmd in self.commands:
            if cmd.name == name:
                return cmd
        return None

    def link(self, name: str) -> Link | None:
        for link in self.links:
            if link.name == name:
                return link
        return None

    def install(self, name: str) -> Install | None:
        for inst in self.installs:
            if inst.name == name:
                return inst
        return None

    def item(self, ref: Ref) -> Command | Link | Install | None:
        kind, name = ref
        if kind == "command":
            return self.command(name)
        if kind == "link":
            return self.link(name)
        return self.install(name)

    def refs(self) -> list[Ref]:
        """Every declared item, installs first, then commands, then links."""
        out: list[Ref] = [("install", i.name) for i in self.installs]
        out += [("command", c.name) for c in self.commands]
        out += [("link", link.name) for link in self.links]
        return out

    def execution_order(self) -> list[Ref]:
        """Items in the order install/sync materializes them.

        Every item comes after what it `requires`; otherwise installs come
        first, then commands, then links, each in declaration order. A
        requirement naming something this config does not hold (because the
        user declined it) imposes no ordering. Cycles are rejected at load
        time, so this always terminates with every item placed.
        """
        pending = self.refs()
        present = set(pending)
        placed: list[Ref] = []
        done: set[Ref] = set()
        while pending:
            for i, ref in enumerate(pending):
                item = self.item(ref)
                deps = item.requires if item is not None else ()
                if all(dep in done or dep not in present for dep in deps):
                    placed.append(ref)
                    done.add(ref)
                    del pending[i]
                    break
            else:  # pragma: no cover - load_project_config rejects cycles
                raise ConfigError(f"{self.name}: requires cycle among {pending}")
        return placed

    def without(
        self,
        *,
        commands: Collection[str] = (),
        links: Collection[str] = (),
        installs: Collection[str] = (),
    ) -> ProjectConfig:
        """Return a copy with the named commands, links, and installs dropped.

        `ProjectConfig` is frozen, so this is the sanctioned way to narrow one —
        it is how a user's declined items are kept out of everything downstream.
        Names that this project does not declare are ignored: stored exclusions
        may name something a later spg.toml no longer declares, and that is not
        an error.
        """
        drop_commands = set(commands)
        drop_links = set(links)
        drop_installs = set(installs)
        if not drop_commands and not drop_links and not drop_installs:
            return self
        return replace(
            self,
            commands=tuple(c for c in self.commands if c.name not in drop_commands),
            links=tuple(link for link in self.links if link.name not in drop_links),
            installs=tuple(i for i in self.installs if i.name not in drop_installs),
        )


_SELECTOR_PREFIXES: dict[str, SelectorKind] = {
    "cmd": "command",
    "link": "link",
    "install": "install",
}
_KIND_PREFIXES: dict[SelectorKind, str] = {v: k for k, v in _SELECTOR_PREFIXES.items()}
_KIND_TABLES: dict[SelectorKind, str] = {
    "command": "commands",
    "link": "links",
    "install": "installs",
}
_ALL_KINDS: tuple[SelectorKind, ...] = ("command", "link", "install")


def display_selector(kind: SelectorKind, name: str) -> str:
    """Render one declarable item the way the user types it: `cmd:x` / `link:y` / `install:z`."""
    return f"{_KIND_PREFIXES[kind]}:{name}"


def resolve_selector(
    config: ProjectConfig,
    selector: str,
    *,
    also_commands: Collection[str] = (),
    also_links: Collection[str] = (),
    also_installs: Collection[str] = (),
) -> tuple[SelectorKind, str]:
    """Resolve a user-typed selector to the namespace and name it refers to.

    Accepts `cmd:<name>`, `link:<name>`, `install:<name>`, or a bare `<name>`
    when only one namespace declares it. ':' is safe as a separator: no item
    name may contain one. Raises `ConfigError` for a typo, an
    ambiguous bare name, or a malformed selector — a freshly typed selector is
    rejected loudly, unlike a stored exclusion.

    `also_commands` / `also_links` / `also_installs` widen each namespace with
    names the config does not declare. `spg enable` passes the stored exclusions, so a user can
    still name (and clear) a decline for something a later spg.toml dropped.
    """
    known: dict[SelectorKind, list[str]] = {
        "command": _known_names([c.name for c in config.commands], also_commands),
        "link": _known_names([link.name for link in config.links], also_links),
        "install": _known_names([i.name for i in config.installs], also_installs),
    }
    raw = selector.strip()
    if not raw:
        raise ConfigError(
            f"empty selector; name a command or link declared in {config.name}'s "
            f"{PROJECT_CONFIG_FILENAME} (e.g. 'cmd:build' or 'link:my-skill')"
        )
    if ":" in raw:
        prefix, _, name = raw.partition(":")
        kind = _SELECTOR_PREFIXES.get(prefix)
        if kind is None:
            raise ConfigError(
                f"invalid selector {selector!r}: unknown prefix {prefix + ':'!r} "
                "(use 'cmd:<name>', 'link:<name>', 'install:<name>', or a bare name)"
            )
        if not name:
            raise ConfigError(
                f"invalid selector {selector!r}: nothing after {prefix + ':'!r} "
                f"(write '{prefix}:<name>')"
            )
        if name in known[kind]:
            return (kind, name)
        raise ConfigError(_unknown_selector_message(config.name, known, name, kinds=(kind,)))

    matches = [kind for kind in _ALL_KINDS if raw in known[kind]]
    if len(matches) > 1:
        tables = " and ".join(f"[{_KIND_TABLES[k]}.{raw}]" for k in matches)
        both = "both " if len(matches) == 2 else ""
        options = " or ".join(f"'{display_selector(k, raw)}'" for k in matches)
        raise ConfigError(
            f"{raw!r} is ambiguous: {config.name}'s {PROJECT_CONFIG_FILENAME} declares {both}"
            f"{tables}; write {options}"
        )
    if matches:
        return (matches[0], raw)
    raise ConfigError(_unknown_selector_message(config.name, known, raw, kinds=_ALL_KINDS))


def _known_names(declared: list[str], also: Collection[str]) -> list[str]:
    """Declared names first, then any extra ones they don't already cover."""
    return declared + [name for name in dict.fromkeys(also) if name not in declared]


def _unknown_selector_message(
    project: str,
    known: dict[SelectorKind, list[str]],
    name: str,
    *,
    kinds: tuple[SelectorKind, ...],
) -> str:
    tables = " or ".join(f"[{_KIND_TABLES[k]}.{name}]" for k in kinds)
    message = f"{project}'s {PROJECT_CONFIG_FILENAME} declares no {tables}"
    pool = [n for kind in kinds for n in known[kind]]
    suggestions = get_close_matches(name, pool, n=3, cutoff=0.7)
    if suggestions:
        seen: list[str] = []
        for suggestion in suggestions:
            for kind in kinds:
                if suggestion in known[kind]:
                    label = display_selector(kind, suggestion) if len(kinds) > 1 else suggestion
                    if label not in seen:
                        seen.append(label)
        message += "; did you mean: " + ", ".join(seen)
    return message


def load_project_config(config_path: Path) -> ProjectConfig:
    if not config_path.is_file():
        raise ConfigError(f"{config_path} not found")

    raw = _load_toml(config_path)

    project_section = raw.get("project")
    if not isinstance(project_section, dict):
        raise ConfigError(f"{config_path}: missing [project] section")
    name = project_section.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(
            f"{config_path}: [project].name is required and must be a non-empty string"
        )
    name = name.strip()
    if not _PROJECT_NAME_RE.match(name):
        raise ConfigError(
            f"{config_path}: [project].name {name!r} is invalid "
            "(must start with a letter or '_' and contain only letters, digits, '_', '-', '.')"
        )

    root = config_path.parent.resolve()
    collector = _Collector()
    collector.add_file(config_path, raw)

    included = _resolve_includes(config_path, root, project_section.get("include", []))
    for include_path in included:
        include_raw = _load_toml(include_path)
        if "project" in include_raw:
            raise ConfigError(
                f"{include_path}: an included file cannot declare [project]; "
                f"only {config_path} may (it is included from there)"
            )
        collector.add_file(include_path, include_raw)

    claimed: dict[Path, str] = {}
    for link in collector.links:
        owner = claimed.get(link.link_path)
        if owner is not None:
            raise ConfigError(
                f"{collector.origin[('link', link.name)]}: [links.{link.name}] and "
                f"[links.{owner}] both resolve to {link.link_path}"
            )
        claimed[link.link_path] = link.name

    config = ProjectConfig(
        name=name,
        root=root,
        commands=tuple(collector.commands),
        links=tuple(collector.links),
        installs=tuple(collector.installs),
        included_files=tuple(included),
    )
    config = _resolve_requires(config, collector)
    _check_requires_cycles(config, collector)
    return config


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc.strerror or exc}") from None


_GLOB_CHARS = frozenset("*?[")


def _resolve_includes(config_path: Path, root: Path, raw: Any) -> list[Path]:
    """Expand `[project].include` into the files to load, in load order.

    Each entry is a path or glob relative to the project root. A literal path
    must exist; a glob may match nothing (an empty `spg.d/` is fine). Matches
    of one glob load in sorted order, entries load in the order written, and a
    file matched twice loads once.
    """
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError(f"{config_path}: [project].include must be a list of strings")
    main = config_path.resolve()
    seen: set[Path] = {main}
    out: list[Path] = []
    for entry in raw:
        pattern = entry.strip()
        if not pattern:
            raise ConfigError(f"{config_path}: [project].include entries must be non-empty")
        if pattern.startswith("~") or Path(pattern).is_absolute():
            raise ConfigError(
                f"{config_path}: [project].include entry {pattern!r} must be relative "
                "to the project root"
            )
        if ".." in Path(pattern).parts:
            raise ConfigError(
                f"{config_path}: [project].include entry {pattern!r} must not contain '..' "
                "(included files have to live inside the project)"
            )
        if _GLOB_CHARS.isdisjoint(pattern):
            path = root / pattern
            if not path.is_file():
                raise ConfigError(
                    f"{config_path}: [project].include names {pattern!r}, but {path} is not a file"
                )
            matches = [path]
        else:
            matches = sorted(p for p in root.glob(pattern) if p.is_file())
        for match in matches:
            resolved = match.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(match)
    return out


class _Collector:
    """Accumulates declarations across spg.toml and its included files.

    Remembers which file declared each item, so a duplicate across files and a
    bad `requires` can both be reported against the file the user must edit.
    """

    def __init__(self) -> None:
        self.commands: list[Command] = []
        self.links: list[Link] = []
        self.installs: list[Install] = []
        self.origin: dict[Ref, Path] = {}
        self.raw_requires: dict[Ref, list[str]] = {}

    def add_file(self, path: Path, raw: dict[str, Any]) -> None:
        for kind in ("install", "command", "link"):
            table = _KIND_TABLES[kind]
            section = raw.get(table, {})
            if not isinstance(section, dict):
                raise ConfigError(f"{path}: [{table}] must be a table")
            for item_name, body in section.items():
                ref: Ref = (kind, item_name)
                previous = self.origin.get(ref)
                if previous is not None:
                    raise ConfigError(
                        f"{path}: [{table}.{item_name}] is already declared in {previous}"
                    )
                if kind == "command":
                    self.commands.append(_parse_command(path, item_name, body))
                elif kind == "link":
                    self.links.append(_parse_link(path, item_name, body))
                else:
                    self.installs.append(_parse_install(path, item_name, body))
                self.origin[ref] = path
                self.raw_requires[ref] = _parse_requires_raw(path, f"{table}.{item_name}", body)


def _parse_requires_raw(path: Path, label: str, body: dict[str, Any]) -> list[str]:
    raw = body.get("requires", [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError(f"{path}: [{label}].requires must be a list of strings")
    return raw


def _resolve_requires(config: ProjectConfig, collector: _Collector) -> ProjectConfig:
    """Resolve every item's `requires` selectors against the whole project."""
    resolved: dict[Ref, tuple[Ref, ...]] = {}
    for ref, selectors in collector.raw_requires.items():
        if not selectors:
            continue
        label = f"[{_KIND_TABLES[ref[0]]}.{ref[1]}].requires"
        deps: list[Ref] = []
        for selector in selectors:
            try:
                dep = resolve_selector(config, selector)
            except ConfigError as exc:
                raise ConfigError(f"{collector.origin[ref]}: {label}: {exc}") from None
            if dep == ref:
                raise ConfigError(
                    f"{collector.origin[ref]}: {label}: an item cannot require itself"
                )
            if dep not in deps:
                deps.append(dep)
        resolved[ref] = tuple(deps)
    if not resolved:
        return config
    return replace(
        config,
        commands=tuple(
            replace(c, requires=resolved.get(("command", c.name), ())) for c in config.commands
        ),
        links=tuple(
            replace(link, requires=resolved.get(("link", link.name), ())) for link in config.links
        ),
        installs=tuple(
            replace(i, requires=resolved.get(("install", i.name), ())) for i in config.installs
        ),
    )


def _check_requires_cycles(config: ProjectConfig, collector: _Collector) -> None:
    """Reject a `requires` cycle, naming the loop the way a user would type it."""
    state: dict[Ref, Literal["visiting", "done"]] = {}
    stack: list[Ref] = []

    def visit(ref: Ref) -> None:
        mark = state.get(ref)
        if mark == "done":
            return
        if mark == "visiting":
            loop = [*stack[stack.index(ref) :], ref]
            rendered = " -> ".join(display_selector(*r) for r in loop)
            raise ConfigError(f"{collector.origin[loop[0]]}: requires cycle: {rendered}")
        state[ref] = "visiting"
        stack.append(ref)
        item = config.item(ref)
        for dep in item.requires if item is not None else ():
            visit(dep)
        stack.pop()
        state[ref] = "done"

    for ref in config.refs():
        visit(ref)


def _parse_command(config_path: Path, cmd_name: str, body: Any) -> Command:
    if not _COMMAND_NAME_RE.match(cmd_name):
        raise ConfigError(
            f"{config_path}: invalid command name {cmd_name!r} "
            "(must start with a letter/underscore and contain only letters, digits, '_', '-', '.')"
        )
    if not isinstance(body, dict):
        raise ConfigError(f"{config_path}: [commands.{cmd_name}] must be a table")

    run = body.get("run", "")
    if not isinstance(run, str):
        raise ConfigError(f"{config_path}: [commands.{cmd_name}].run must be a string")

    shell_function = body.get("shell_function", "")
    if not isinstance(shell_function, str):
        raise ConfigError(f"{config_path}: [commands.{cmd_name}].shell_function must be a string")

    has_run = bool(run.strip())
    has_fn = bool(shell_function.strip())
    if has_run and has_fn:
        raise ConfigError(
            f"{config_path}: [commands.{cmd_name}] cannot set both 'run' and 'shell_function'"
        )
    if not has_run and not has_fn:
        raise ConfigError(
            f"{config_path}: [commands.{cmd_name}] must set 'run' or 'shell_function'"
        )

    description = body.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"{config_path}: [commands.{cmd_name}].description must be a string")

    args_raw = body.get("args", [])
    if not isinstance(args_raw, list):
        raise ConfigError(f"{config_path}: [commands.{cmd_name}].args must be an array")

    args: list[CommandArg] = []
    for i, raw_arg in enumerate(args_raw):
        if not isinstance(raw_arg, dict):
            raise ConfigError(
                f"{config_path}: [commands.{cmd_name}].args[{i}] must be a table "
                "with 'name' and optional 'description'"
            )
        arg_dict = cast(dict[str, Any], raw_arg)
        arg_name = arg_dict.get("name")
        if not isinstance(arg_name, str) or not arg_name.strip():
            raise ConfigError(f"{config_path}: [commands.{cmd_name}].args[{i}].name is required")
        arg_desc = arg_dict.get("description", "")
        if not isinstance(arg_desc, str):
            raise ConfigError(
                f"{config_path}: [commands.{cmd_name}].args[{i}].description must be a string"
            )
        arg_type = arg_dict.get("type", "")
        if not isinstance(arg_type, str):
            raise ConfigError(
                f"{config_path}: [commands.{cmd_name}].args[{i}].type must be a string"
            )
        if arg_type and arg_type not in _ARG_TYPES:
            raise ConfigError(
                f"{config_path}: [commands.{cmd_name}].args[{i}].type must be one of "
                f"{_ARG_TYPES} (got {arg_type!r})"
            )
        arg_values_raw = arg_dict.get("values", [])
        if not isinstance(arg_values_raw, list) or not all(
            isinstance(v, str) for v in arg_values_raw
        ):
            raise ConfigError(
                f"{config_path}: [commands.{cmd_name}].args[{i}].values must be a list of strings"
            )
        if arg_type and arg_values_raw:
            raise ConfigError(
                f"{config_path}: [commands.{cmd_name}].args[{i}] cannot set both "
                "'type' and 'values'"
            )
        args.append(
            CommandArg(
                name=arg_name.strip(),
                description=arg_desc,
                type=arg_type,
                values=tuple(arg_values_raw),
            )
        )

    complete_hook = body.get("complete_hook", "")
    if not isinstance(complete_hook, str):
        raise ConfigError(f"{config_path}: [commands.{cmd_name}].complete_hook must be a string")

    return Command(
        name=cmd_name,
        run=run.strip(),
        description=description,
        args=tuple(args),
        complete_hook=complete_hook.strip(),
        shell_function=shell_function.strip(),
    )


def _parse_link(config_path: Path, link_name: str, body: Any) -> Link:
    if link_name in (".", "..") or not _LINK_NAME_RE.match(link_name):
        raise ConfigError(
            f"{config_path}: invalid link name {link_name!r} "
            "(must contain only letters, digits, '_', '-', '.' and cannot be '.' or '..')"
        )
    if not isinstance(body, dict):
        raise ConfigError(f"{config_path}: [links.{link_name}] must be a table")

    source = body.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ConfigError(
            f"{config_path}: [links.{link_name}].source is required and must be a non-empty string"
        )
    source = source.strip()
    if source.startswith("~") or Path(source).is_absolute():
        raise ConfigError(
            f"{config_path}: [links.{link_name}].source {source!r} must be a path relative "
            "to the project root"
        )
    if ".." in Path(source).parts:
        raise ConfigError(
            f"{config_path}: [links.{link_name}].source {source!r} must not contain '..' "
            "(a link source has to live inside the project)"
        )

    target = body.get("target")
    if not isinstance(target, str) or not target.strip():
        raise ConfigError(
            f"{config_path}: [links.{link_name}].target is required and must be a non-empty string"
        )
    target = target.strip()
    if not Path(target).expanduser().is_absolute():
        raise ConfigError(
            f"{config_path}: [links.{link_name}].target {target!r} must be an absolute path "
            "(it may start with '~'). End it with '/' to link into a directory."
        )

    description = body.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"{config_path}: [links.{link_name}].description must be a string")

    return Link(name=link_name, source=source, target=target, description=description)


_BREW_LOCATE = (
    "if command -v brew >/dev/null 2>&1; then BREW=brew; "
    "elif [ -x /opt/homebrew/bin/brew ]; then BREW=/opt/homebrew/bin/brew; "
    "elif [ -x /usr/local/bin/brew ]; then BREW=/usr/local/bin/brew; "
    'else echo "spg: brew not found on PATH, /opt/homebrew/bin, or /usr/local/bin" >&2; '
    "exit 127; fi"
)


def _brew_scripts(formulae: tuple[str, ...], casks: tuple[str, ...]) -> tuple[str, str]:
    """Generate (run, check) shell for the `brew`/`cask` shorthands.

    `brew` is located explicitly because right after a fresh Homebrew install
    it is not yet on PATH. `check` passes only when every listed package is
    installed; `run` installs them all (brew skips ones already present).
    """
    run_steps: list[str] = []
    check_steps: list[str] = []
    for flag, packages in (("--formula", formulae), ("--cask", casks)):
        if not packages:
            continue
        quoted = " ".join(shlex.quote(p) for p in packages)
        install_flag = " --cask" if flag == "--cask" else ""
        run_steps.append(f'"$BREW" install{install_flag} {quoted}')
        check_steps.append(
            f'for p in {quoted}; do "$BREW" list {flag} "$p" >/dev/null 2>&1 || exit 1; done'
        )
    run = f"{_BREW_LOCATE}; " + " && ".join(run_steps)
    check = f"{_BREW_LOCATE}; " + "; ".join(check_steps)
    return run, check


def _parse_install(config_path: Path, install_name: str, body: Any) -> Install:
    if not _COMMAND_NAME_RE.match(install_name):
        raise ConfigError(
            f"{config_path}: invalid install name {install_name!r} "
            "(must start with a letter/underscore and contain only letters, digits, '_', '-', '.')"
        )
    label = f"[installs.{install_name}]"
    if not isinstance(body, dict):
        raise ConfigError(f"{config_path}: {label} must be a table")

    def string_field(key: str) -> str:
        value = body.get(key, "")
        if not isinstance(value, str):
            raise ConfigError(f"{config_path}: {label}.{key} must be a string")
        return value.strip()

    def packages_field(key: str) -> tuple[str, ...]:
        value = body.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(v, str) and v.strip() and not any(ch.isspace() for ch in v.strip())
            for v in value
        ):
            raise ConfigError(
                f"{config_path}: {label}.{key} must be a list of package names "
                "(non-empty strings without whitespace)"
            )
        return tuple(v.strip() for v in value)

    run = string_field("run")
    check = string_field("check")
    creates = string_field("creates")
    description = body.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"{config_path}: {label}.description must be a string")
    brew = packages_field("brew")
    cask = packages_field("cask")

    if brew or cask:
        clashing = [
            key for key, value in (("run", run), ("check", check), ("creates", creates)) if value
        ]
        if clashing:
            raise ConfigError(
                f"{config_path}: {label} sets 'brew'/'cask', which generate the install and "
                f"its check; drop {', '.join(repr(k) for k in clashing)}"
            )
        run, check = _brew_scripts(brew, cask)
        return Install(
            name=install_name,
            run=run,
            check=check,
            description=description,
            brew=brew,
            cask=cask,
        )

    if not run:
        raise ConfigError(f"{config_path}: {label} must set 'run' (or the 'brew'/'cask' shorthand)")
    if check and creates:
        raise ConfigError(f"{config_path}: {label} cannot set both 'check' and 'creates'")
    if not check and not creates:
        raise ConfigError(
            f"{config_path}: {label} must set 'check' (a shell command that exits 0 when "
            "already installed) or 'creates' (a path that exists once installed), "
            "so spg does not rerun the installer on every install and sync"
        )
    if creates and not Path(creates).expanduser().is_absolute():
        raise ConfigError(
            f"{config_path}: {label}.creates {creates!r} must be an absolute path "
            "(it may start with '~')"
        )
    return Install(
        name=install_name,
        run=run,
        check=check,
        creates=creates,
        description=description,
    )


def load_project_config_from_dir(directory: Path) -> ProjectConfig:
    return load_project_config(directory / PROJECT_CONFIG_FILENAME)
