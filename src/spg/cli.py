from __future__ import annotations

from pathlib import Path
from typing import Sequence

import click as _click
import rich_click as click
from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

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

# Shared rich consoles. `console` writes to stdout for normal command output;
# `err_console` writes to stderr for warnings/errors. Both auto-disable styling
# when output is not a terminal (e.g. piped or captured under pytest).
console = Console()
err_console = Console(stderr=True)


def _short_path(path: Path | str) -> str:
    """Render a path with the user's home directory collapsed to ``~``."""
    text = str(path)
    try:
        home = str(Path.home())
    except (RuntimeError, OSError):
        return text
    if text == home:
        return "~"
    if text.startswith(home + "/"):
        return "~" + text[len(home):]
    return text


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
        err_console.print(f"[red]spg:[/] {target_dir} is not a directory")
        return 1
    target = target_dir / PROJECT_CONFIG_FILENAME
    if target.exists():
        err_console.print(f"[red]spg:[/] {target} already exists")
        return 1
    project_name = name_ or target_dir.name
    target.write_text(STARTER_TEMPLATE.replace("__SPG_NAME__", project_name))
    console.print(f"[bold green]✓[/] Wrote [bold]{_short_path(target)}[/].")
    console.print("Edit it to declare commands, then run [bold cyan]spg install[/].")
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
    console.print(f"[bold green]✓[/] Uninstalled [bold cyan]{result.project}[/].")
    if result.removed:
        _print_change_line("-", "red", "removed", result.removed)
    if result.skipped:
        _print_change_line("!", "yellow", "skipped", result.skipped)
        console.print("  [dim](skipped entries are not spg-managed)[/]")
    return 0


@cli.command()
def sync() -> int:
    """Re-read every registered project's spg.toml and refresh ~/bin wrappers."""
    registry = Registry.load(registry_path())
    if not registry.projects:
        console.print("[dim]No projects registered.[/]")
        return 0
    any_failures = False
    for entry in list(registry):
        config_file = entry.root / PROJECT_CONFIG_FILENAME
        if not config_file.is_file():
            err_console.print(f"[yellow]warn:[/] {entry.name}: {config_file} is missing; skipping")
            any_failures = True
            continue
        try:
            config = load_project_config(config_file)
        except ConfigError as exc:
            err_console.print(f"[yellow]warn:[/] {entry.name}: {exc}")
            any_failures = True
            continue
        try:
            result = sync_project(config, registry, bin_dir())
        except InstallError as exc:
            err_console.print(f"[yellow]warn:[/] {entry.name}: {exc}")
            any_failures = True
            continue
        _print_install_result(result, verb="Synced")
    return 1 if any_failures else 0


@cli.command(name="list")
def list_projects() -> int:
    """Show registered projects and their commands."""
    registry = Registry.load(registry_path())
    if not registry.projects:
        console.print("[dim]No projects registered.[/] Run [bold cyan]spg install[/] in a project.")
        return 0

    entries = sorted(registry, key=lambda e: e.name)
    total_commands = sum(len(e.commands) for e in entries)

    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False, pad_edge=False)
    table.add_column("Project", style="bold cyan", no_wrap=True)
    table.add_column("Commands", style="green")
    table.add_column("Root", style="dim", overflow="fold")
    for entry in entries:
        commands = (
            Text("  ").join(Text(c, style="green") for c in entry.commands)
            if entry.commands
            else Text("(none)", style="dim italic")
        )
        table.add_row(entry.name, commands, _short_path(entry.root))

    console.print(table)
    project_word = "project" if len(entries) == 1 else "projects"
    command_word = "command" if total_commands == 1 else "commands"
    console.print(
        f"[dim]{len(entries)} {project_word}, {total_commands} {command_word}.[/]"
    )
    return 0


@cli.command(name="help")
@click.argument("name")
def help_(name: str) -> int:
    """Show usage for a command exposed via spg."""
    registry = Registry.load(registry_path())
    owner = registry.find_owner_of_command(name)
    if owner is None:
        err_console.print(f"[red]spg:[/] no registered command named [bold]{name}[/]")
        return 1
    config_file = owner.root / PROJECT_CONFIG_FILENAME
    if not config_file.is_file():
        err_console.print(f"[red]spg:[/] {owner.name}: {config_file} is missing")
        return 1
    config = load_project_config(config_file)
    cmd = config.command(name)
    if cmd is None:
        err_console.print(
            f"[red]spg:[/] registry says [bold]{owner.name}[/] owns [bold]{name}[/], but it's "
            f"no longer in {config_file}. Run [bold cyan]spg sync[/]."
        )
        return 1

    body: list[RenderableType] = []
    if cmd.description:
        body.append(Text(cmd.description, style="italic"))

    if cmd.args:
        args_table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
        args_table.add_column(style="yellow", no_wrap=True)
        args_table.add_column(overflow="fold")
        for a in cmd.args:
            extras = []
            if a.values:
                extras.append("one of: " + ", ".join(a.values))
            elif a.type:
                extras.append(a.type)
            desc = Text(a.description or "")
            if extras:
                if desc.plain:
                    desc.append(" ")
                desc.append("(" + "; ".join(extras) + ")", style="dim")
            args_table.add_row(a.name, desc)
        if body:
            body.append(Text())
        body.append(Text("Arguments", style="bold"))
        body.append(args_table)

    detail = Text()
    if cmd.is_shell_function:
        detail.append("Shell function", style="bold")
        detail.append(f"  (sourced at shell start, from {_short_path(owner.root)})", style="dim")
        for line in cmd.shell_function.splitlines() or [""]:
            detail.append("\n  ")
            detail.append(line, style="green")
    else:
        detail.append("Runs", style="bold")
        detail.append("  ")
        detail.append(str(cmd.run), style="green")
        detail.append(f"  (from {_short_path(owner.root)})", style="dim")
    if cmd.complete_hook:
        detail.append("\n")
        detail.append("Completion hook", style="bold")
        detail.append("  ")
        detail.append(cmd.complete_hook, style="green")
    if body:
        body.append(Text())
    body.append(detail)

    title = Text()
    title.append(cmd.name, style="bold cyan")
    title.append(f"  ({owner.name})", style="dim")
    console.print(Panel(Group(*body), title=title, title_align="left", box=box.ROUNDED, padding=(1, 2)))
    return 0


@cli.command()
def status() -> int:
    """Diagnose registry / ~/bin mismatches."""
    registry = Registry.load(registry_path())
    bd = bin_dir()
    registered = {e.name: e for e in registry}
    wrappers = list_managed_wrappers(bd)

    summary = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
    summary.add_column(style="bold", no_wrap=True)
    summary.add_column(overflow="fold")
    summary.add_row("Registry", _short_path(registry_path()))
    summary.add_row("Bin dir", _short_path(bd))
    summary.add_row("Projects registered", str(len(registered)))
    summary.add_row("Managed wrappers", str(len(wrappers)))
    console.print(summary)

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
        items = Text()
        for i, p in enumerate(problems):
            if i:
                items.append("\n")
            items.append("• ", style="red")
            items.append(p)
        count = "1 problem" if len(problems) == 1 else f"{len(problems)} problems"
        console.print()
        console.print(
            Panel(items, title=f"[bold red]{count}[/]", title_align="left", box=box.ROUNDED, padding=(1, 2))
        )
        return 1
    console.print("\n[bold green]✓[/] All good.")
    return 0


@cli.group(invoke_without_command=True, context_settings=CONTEXT_SETTINGS)
@click.pass_context
def completion(ctx: click.Context) -> int:
    """Print a shell completion script (e.g. `spg completion zsh`)."""
    if ctx.invoked_subcommand is None:
        err_console.print("usage: [bold cyan]spg completion <shell>[/]")
        err_console.print("supported shells: [green]zsh[/]")
        err_console.print("(example: [dim]source <(spg completion zsh)[/] in ~/.zshrc)")
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
        err_console.print(f"[red]spg:[/] {exc}")
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


def _print_change_line(symbol: str, style: str, label: str, names: list[str]) -> None:
    line = Text()
    line.append("  ")
    line.append(f"{symbol} ", style=style)
    line.append(f"{label} ".ljust(12), style="bold")
    line.append(", ".join(names), style=style)
    console.print(line)


def _print_install_result(result: InstallResult, verb: str = "Installed") -> None:
    console.print(f"[bold green]✓[/] {verb} [bold cyan]{result.project}[/].")
    if result.written:
        _print_change_line("+", "green", "added", result.written)
    if result.refreshed:
        _print_change_line("~", "yellow", "refreshed", result.refreshed)
    if result.removed:
        _print_change_line("-", "red", "removed", result.removed)
    if not (result.written or result.refreshed or result.removed):
        console.print("  [dim]no changes.[/]")


if __name__ == "__main__":
    raise SystemExit(main())
