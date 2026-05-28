from __future__ import annotations

from pathlib import Path
from typing import Sequence

import click as _click
import rich_click as click

from spg import __version__
from spg.completion import (
    candidates_for_command,
    candidates_for_spg,
    list_managed_commands,
    render_shell_function_defs,
    render_zsh_completion,
)
from spg.config import (
    ConfigError,
    ProjectConfig,
    load_project_config,
    load_project_config_from_dir,
)
from spg.installer import (
    InstallError,
    InstallResult,
    install_project,
    list_managed_wrappers,
    sync_project,
    uninstall_project,
)
from spg.paths import (
    PROJECT_CONFIG_FILENAME,
    bin_dir,
    find_project_config,
    registry_path,
)
from spg.registry import Registry

STARTER_TEMPLATE = """\
# spg.toml — describes commands this project exposes to ~/bin via spg.

[project]
name = "__SPG_NAME__"

# Each [commands.<name>] block becomes either a ~/bin/<name> wrapper (run = ...)
# or a shell function defined in your interactive shell (shell_function = ...).
# Use `run` for normal commands; use `shell_function` when the command must
# affect the parent shell (cd, set env vars, etc.). Exactly one is required.
#
# [commands.hello]
# run = "./scripts/hello.sh"
# description = "Say hello"
# args = [
#   { name = "who", description = "name to greet" },
# ]
#
# [commands.gocd]
# description = "cd into a project subdirectory"
# shell_function = 'cd "$(./scripts/resolve.sh "$@")"'
"""

click.rich_click.TEXT_MARKUP = "ansi"
click.rich_click.SHOW_ARGUMENTS = True
click.rich_click.GROUP_ARGUMENTS_OPTIONS = True
click.rich_click.STYLE_OPTION = "bold cyan"
click.rich_click.STYLE_SWITCH = "bold green"
click.rich_click.STYLE_METAVAR = "yellow"
click.rich_click.STYLE_HEADER_TEXT = "bold"
click.rich_click.STYLE_COMMANDS_TABLE_SHOW_LINES = False
click.rich_click.MAX_WIDTH = 100

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


@click.group(invoke_without_command=True, context_settings=CONTEXT_SETTINGS)
@click.version_option(__version__, "--version", prog_name="spg", message="%(prog)s %(version)s")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Publish per-project commands declared in spg.toml to ~/bin."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(1)


@cli.command()
@click.option("--name", "name_", default=None, help="Project name to seed in the new config (defaults to directory name).")
@click.option("-C", "--dir", "directory", default=".", show_default=True, help="Project directory.")
def init(name_: str | None, directory: str) -> int:
    """Create a starter spg.toml in the current directory."""
    target_dir = Path(directory).resolve()
    if not target_dir.is_dir():
        click.echo(f"spg: {target_dir} is not a directory", err=True)
        return 1
    target = target_dir / PROJECT_CONFIG_FILENAME
    if target.exists():
        click.echo(f"spg: {target} already exists", err=True)
        return 1
    project_name = name_ or target_dir.name
    target.write_text(STARTER_TEMPLATE.replace("__SPG_NAME__", project_name))
    click.echo(f"Wrote {target}")
    click.echo("Edit it to declare commands, then run `spg install`.")
    return 0


@cli.command()
@click.option("-C", "--dir", "directory", default=".", show_default=True, help="Project directory.")
@click.option("--force", is_flag=True, help="Overwrite non-spg-managed files in ~/bin with matching names.")
def install(directory: str, force: bool) -> int:
    """Register the current project and write wrappers to ~/bin."""
    config = _resolve_config(Path(directory))
    registry = Registry.load(registry_path())
    result = install_project(config, registry, bin_dir(), force=force)
    _print_install_result(result)
    return 0


@cli.command()
@click.argument("name", required=False)
@click.option("-C", "--dir", "directory", default=".", show_default=True, help="Project directory (used when no name is given).")
def uninstall(name: str | None, directory: str) -> int:
    """Remove wrappers and registry entry for a project."""
    registry = Registry.load(registry_path())
    project_name = name
    if project_name is None:
        config = _resolve_config(Path(directory))
        project_name = config.name
    result = uninstall_project(project_name, registry, bin_dir())
    click.echo(f"Uninstalled {result.project}.")
    if result.removed:
        click.echo("  removed: " + ", ".join(result.removed))
    if result.skipped:
        click.echo("  skipped (not spg-managed): " + ", ".join(result.skipped))
    return 0


@cli.command()
def sync() -> int:
    """Re-read every registered project's spg.toml and refresh ~/bin wrappers."""
    registry = Registry.load(registry_path())
    if not registry.projects:
        click.echo("No projects registered.")
        return 0
    any_failures = False
    for entry in list(registry):
        config_file = entry.root / PROJECT_CONFIG_FILENAME
        if not config_file.is_file():
            click.echo(f"warn: {entry.name}: {config_file} is missing; skipping", err=True)
            any_failures = True
            continue
        try:
            config = load_project_config(config_file)
        except ConfigError as exc:
            click.echo(f"warn: {entry.name}: {exc}", err=True)
            any_failures = True
            continue
        try:
            result = sync_project(config, registry, bin_dir())
        except InstallError as exc:
            click.echo(f"warn: {entry.name}: {exc}", err=True)
            any_failures = True
            continue
        _print_install_result(result, verb="Synced")
    return 1 if any_failures else 0


@cli.command(name="list")
def list_projects() -> int:
    """Show registered projects and their commands."""
    registry = Registry.load(registry_path())
    if not registry.projects:
        click.echo("No projects registered.")
        return 0
    for entry in sorted(registry, key=lambda e: e.name):
        commands = ", ".join(entry.commands) if entry.commands else "(none)"
        click.echo(f"{entry.name}  {entry.root}")
        click.echo(f"  commands: {commands}")
    return 0


@cli.command(name="help")
@click.argument("name")
def help_(name: str) -> int:
    """Show usage for a command exposed via spg."""
    registry = Registry.load(registry_path())
    owner = registry.find_owner_of_command(name)
    if owner is None:
        click.echo(f"spg: no registered command named {name!r}", err=True)
        return 1
    config_file = owner.root / PROJECT_CONFIG_FILENAME
    if not config_file.is_file():
        click.echo(f"spg: {owner.name}: {config_file} is missing", err=True)
        return 1
    config = load_project_config(config_file)
    cmd = config.command(name)
    if cmd is None:
        click.echo(
            f"spg: registry says {owner.name!r} owns {name!r}, but it's no longer in {config_file}. "
            f"Run `spg sync`.",
            err=True,
        )
        return 1
    click.echo(f"{cmd.name}  ({owner.name})")
    if cmd.description:
        click.echo(f"  {cmd.description}")
    if cmd.args:
        click.echo("  Arguments:")
        width = max(len(a.name) for a in cmd.args)
        for a in cmd.args:
            extras = []
            if a.values:
                extras.append("one of: " + ", ".join(a.values))
            elif a.type:
                extras.append(a.type)
            desc = a.description or ""
            if extras:
                hint = " (" + "; ".join(extras) + ")"
                desc = f"{desc}{hint}" if desc else hint.lstrip(" ")
            click.echo(f"    {a.name.ljust(width)}  {desc}")
    if cmd.complete_hook:
        click.echo(f"  Completion hook: {cmd.complete_hook}")
    if cmd.is_shell_function:
        click.echo(f"  Shell function (sourced at shell start, from {owner.root}):")
        for line in cmd.shell_function.splitlines() or [""]:
            click.echo(f"    {line}")
    else:
        click.echo(f"  Runs: {cmd.run}  (from {owner.root})")
    return 0


@cli.command()
def status() -> int:
    """Diagnose registry / ~/bin mismatches."""
    registry = Registry.load(registry_path())
    bd = bin_dir()
    registered = {e.name: e for e in registry}
    wrappers = list_managed_wrappers(bd)

    click.echo(f"Registry: {registry_path()}")
    click.echo(f"Bin dir:  {bd}")
    click.echo(f"Projects registered: {len(registered)}")
    click.echo(f"Managed wrappers in ~/bin: {len(wrappers)}")

    by_project: dict[str, list[str]] = {}
    for path, meta in wrappers:
        by_project.setdefault(meta.project, []).append(path.name)

    problems: list[str] = []
    shell_fn_commands: dict[str, set[str]] = {}

    for name, entry in registered.items():
        config_file = entry.root / PROJECT_CONFIG_FILENAME
        try:
            project_config = load_project_config(config_file)
        except (ConfigError, OSError):
            project_config = None
        if project_config is not None:
            shell_fn_commands[name] = {
                c.name for c in project_config.commands if c.is_shell_function
            }
        for cmd in entry.commands:
            if cmd in shell_fn_commands.get(name, set()):
                continue
            wrapper_path = bd / cmd
            if not wrapper_path.exists():
                problems.append(f"missing wrapper: {wrapper_path} (owned by {name})")

    for project, found in by_project.items():
        if project not in registered:
            problems.append(f"orphan wrappers from unregistered project {project!r}: {', '.join(found)}")
            continue
        expected = set(registered[project].commands)
        for cmd in found:
            if cmd not in expected:
                problems.append(f"wrapper {cmd!r} not declared in {project!r}'s spg.toml")

    if problems:
        click.echo("\nProblems:")
        for p in problems:
            click.echo(f"  - {p}")
        return 1
    click.echo("\nAll good.")
    return 0


@cli.group(invoke_without_command=True, context_settings=CONTEXT_SETTINGS)
@click.pass_context
def completion(ctx: click.Context) -> int:
    """Print a shell completion script (e.g. `spg completion zsh`)."""
    if ctx.invoked_subcommand is None:
        click.echo("usage: spg completion <shell>", err=True)
        click.echo("supported shells: zsh", err=True)
        click.echo("(example: `source <(spg completion zsh)` in ~/.zshrc)", err=True)
        return 1
    return 0


@completion.command(name="zsh")
def completion_zsh() -> int:
    """Print a zsh completion script for spg and its managed commands."""
    click.echo(render_zsh_completion())
    return 0


@cli.command(
    name="__complete",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("namespace")
@click.argument("rest", nargs=-1, type=click.UNPROCESSED)
def internal_complete(namespace: str, rest: tuple[str, ...]) -> int:
    rest_list: list[str] = list(rest)
    try:
        registry = Registry.load(registry_path())
    except Exception:
        registry = Registry(path=registry_path())

    if namespace == "list-commands":
        for cmd in list_managed_commands(registry):
            click.echo(cmd)
        return 0
    if namespace == "list-projects":
        for project_name in sorted(registry.projects):
            click.echo(project_name)
        return 0
    if namespace == "list-shell-functions":
        defs = render_shell_function_defs(registry)
        if defs:
            click.echo(defs, nl=False)
        return 0
    if namespace == "spg":
        if not rest_list:
            return 0
        try:
            current = int(rest_list[0])
        except ValueError:
            return 0
        words = rest_list[1:]
        for line in candidates_for_spg(words, current, registry):
            click.echo(line)
        return 0
    if namespace == "cmd":
        if len(rest_list) < 2:
            return 0
        name = rest_list[0]
        try:
            current = int(rest_list[1])
        except ValueError:
            return 0
        words = rest_list[2:]
        for line in candidates_for_command(name, words, current, registry):
            click.echo(line)
        return 0
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv) if argv is not None else None
    try:
        result = cli.main(args=args, prog_name="spg", standalone_mode=False)
    except (ConfigError, InstallError) as exc:
        click.echo(f"spg: {exc}", err=True)
        return 1
    except _click.exceptions.UsageError as exc:
        exc.show()
        return exc.exit_code
    except _click.exceptions.ClickException as exc:
        exc.show()
        return exc.exit_code
    except _click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        return 1
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 1
    if result is None:
        return 0
    if isinstance(result, int):
        return result
    return 0


def _resolve_config(start: Path) -> ProjectConfig:
    start = start.resolve()
    if start.is_dir():
        config_file = find_project_config(start)
        if config_file is None:
            raise ConfigError(f"no {PROJECT_CONFIG_FILENAME} found at or above {start}")
        return load_project_config(config_file)
    return load_project_config_from_dir(start.parent)


def _print_install_result(result: InstallResult, verb: str = "Installed") -> None:
    click.echo(f"{verb} {result.project}.")
    if result.written:
        click.echo("  added:     " + ", ".join(result.written))
    if result.refreshed:
        click.echo("  refreshed: " + ", ".join(result.refreshed))
    if result.removed:
        click.echo("  removed:   " + ", ".join(result.removed))


if __name__ == "__main__":
    raise SystemExit(main())
