from __future__ import annotations

import re
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
class ProjectConfig:
    name: str
    root: Path
    commands: tuple[Command, ...] = field(default_factory=tuple)
    links: tuple[Link, ...] = field(default_factory=tuple)

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

    def without(
        self,
        *,
        commands: Collection[str] = (),
        links: Collection[str] = (),
    ) -> ProjectConfig:
        """Return a copy with the named commands and links dropped.

        `ProjectConfig` is frozen, so this is the sanctioned way to narrow one —
        it is how a user's declined items are kept out of everything downstream.
        Names that this project does not declare are ignored: stored exclusions
        may name something a later spg.toml no longer declares, and that is not
        an error.
        """
        drop_commands = set(commands)
        drop_links = set(links)
        if not drop_commands and not drop_links:
            return self
        return replace(
            self,
            commands=tuple(c for c in self.commands if c.name not in drop_commands),
            links=tuple(link for link in self.links if link.name not in drop_links),
        )


# Which namespace a selector names. Command names and link names live in
# separate tables and may collide, so a bare name is not always enough.
SelectorKind = Literal["command", "link"]

_SELECTOR_PREFIXES: dict[str, SelectorKind] = {"cmd": "command", "link": "link"}
_KIND_TABLES: dict[SelectorKind, str] = {"command": "commands", "link": "links"}


def display_selector(kind: SelectorKind, name: str) -> str:
    """Render one declarable item the way the user types it: `cmd:x` / `link:y`."""
    return f"cmd:{name}" if kind == "command" else f"link:{name}"


def resolve_selector(
    config: ProjectConfig,
    selector: str,
    *,
    also_commands: Collection[str] = (),
    also_links: Collection[str] = (),
) -> tuple[SelectorKind, str]:
    """Resolve a user-typed selector to the namespace and name it refers to.

    Accepts `cmd:<name>`, `link:<name>`, or a bare `<name>` when only one
    namespace declares it. ':' is safe as a separator: neither a command name
    nor a link name may contain one. Raises `ConfigError` for a typo, an
    ambiguous bare name, or a malformed selector — a freshly typed selector is
    rejected loudly, unlike a stored exclusion.

    `also_commands` / `also_links` widen each namespace with names the config
    does not declare. `spg enable` passes the stored exclusions, so a user can
    still name (and clear) a decline for something a later spg.toml dropped.
    """
    known: dict[SelectorKind, list[str]] = {
        "command": _known_names([c.name for c in config.commands], also_commands),
        "link": _known_names([link.name for link in config.links], also_links),
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
                "(use 'cmd:<name>', 'link:<name>', or a bare name)"
            )
        if not name:
            raise ConfigError(
                f"invalid selector {selector!r}: nothing after {prefix + ':'!r} "
                f"(write '{prefix}:<name>')"
            )
        if name in known[kind]:
            return (kind, name)
        raise ConfigError(_unknown_selector_message(config.name, known, name, kinds=(kind,)))

    is_command = raw in known["command"]
    is_link = raw in known["link"]
    if is_command and is_link:
        raise ConfigError(
            f"{raw!r} is ambiguous: {config.name}'s {PROJECT_CONFIG_FILENAME} declares both "
            f"[commands.{raw}] and [links.{raw}]; write 'cmd:{raw}' or 'link:{raw}'"
        )
    if is_command:
        return ("command", raw)
    if is_link:
        return ("link", raw)
    raise ConfigError(_unknown_selector_message(config.name, known, raw, kinds=("command", "link")))


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

    with config_path.open("rb") as f:
        raw: dict[str, Any] = tomllib.load(f)

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

    commands_section = raw.get("commands", {})
    if not isinstance(commands_section, dict):
        raise ConfigError(f"{config_path}: [commands] must be a table")

    commands: list[Command] = []
    for cmd_name, body in commands_section.items():
        commands.append(_parse_command(config_path, cmd_name, body))

    links_section = raw.get("links", {})
    if not isinstance(links_section, dict):
        raise ConfigError(f"{config_path}: [links] must be a table")

    links: list[Link] = []
    for link_name, body in links_section.items():
        links.append(_parse_link(config_path, link_name, body))

    claimed: dict[Path, str] = {}
    for link in links:
        owner = claimed.get(link.link_path)
        if owner is not None:
            raise ConfigError(
                f"{config_path}: [links.{link.name}] and [links.{owner}] both resolve to "
                f"{link.link_path}"
            )
        claimed[link.link_path] = link.name

    return ProjectConfig(
        name=name,
        root=config_path.parent.resolve(),
        commands=tuple(commands),
        links=tuple(links),
    )


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


def load_project_config_from_dir(directory: Path) -> ProjectConfig:
    return load_project_config(directory / PROJECT_CONFIG_FILENAME)
