from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from spg.paths import PROJECT_CONFIG_FILENAME

_COMMAND_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


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
class ProjectConfig:
    name: str
    root: Path
    commands: tuple[Command, ...] = field(default_factory=tuple)

    def command(self, name: str) -> Command | None:
        for cmd in self.commands:
            if cmd.name == name:
                return cmd
        return None


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
        raise ConfigError(f"{config_path}: [project].name is required and must be a non-empty string")
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

    return ProjectConfig(
        name=name,
        root=config_path.parent.resolve(),
        commands=tuple(commands),
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
        raise ConfigError(
            f"{config_path}: [commands.{cmd_name}].shell_function must be a string"
        )

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
                f"{config_path}: [commands.{cmd_name}].args[{i}] must be a table with 'name' and optional 'description'"
            )
        arg_dict = cast(dict[str, Any], raw_arg)
        arg_name = arg_dict.get("name")
        if not isinstance(arg_name, str) or not arg_name.strip():
            raise ConfigError(f"{config_path}: [commands.{cmd_name}].args[{i}].name is required")
        arg_desc = arg_dict.get("description", "")
        if not isinstance(arg_desc, str):
            raise ConfigError(f"{config_path}: [commands.{cmd_name}].args[{i}].description must be a string")
        arg_type = arg_dict.get("type", "")
        if not isinstance(arg_type, str):
            raise ConfigError(f"{config_path}: [commands.{cmd_name}].args[{i}].type must be a string")
        if arg_type and arg_type not in _ARG_TYPES:
            raise ConfigError(
                f"{config_path}: [commands.{cmd_name}].args[{i}].type must be one of {_ARG_TYPES} (got {arg_type!r})"
            )
        arg_values_raw = arg_dict.get("values", [])
        if not isinstance(arg_values_raw, list) or not all(isinstance(v, str) for v in arg_values_raw):
            raise ConfigError(
                f"{config_path}: [commands.{cmd_name}].args[{i}].values must be a list of strings"
            )
        if arg_type and arg_values_raw:
            raise ConfigError(
                f"{config_path}: [commands.{cmd_name}].args[{i}] cannot set both 'type' and 'values'"
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


def load_project_config_from_dir(directory: Path) -> ProjectConfig:
    return load_project_config(directory / PROJECT_CONFIG_FILENAME)
