from __future__ import annotations

import sys
from collections.abc import Collection, Sequence
from difflib import get_close_matches
from pathlib import Path

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
    SelectorKind,
    display_selector,
    load_project_config,
    load_project_config_from_dir,
    resolve_selector,
)
from spg.installer import (
    ExclusionChange,
    InstallError,
    InstallResult,
    entry_excluded_selectors,
    install_project,
    link_state,
    list_managed_wrappers,
    prune_orphan_wrappers,
    sync_project,
    uninstall_project,
)
from spg.paths import (
    PROJECT_CONFIG_FILENAME,
    bin_dir,
    find_project_config,
    registry_path,
    resolve_path,
)
from spg.registry import Registry, RegistryEntry, RegistryError

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

# Each [links.<name>] block symlinks a path in this repo to somewhere on your
# machine. `source` is relative to the repo root. A `target` ending in '/' means
# "link into that directory" (the leaf name is <name>); otherwise `target` is
# the exact path of the symlink.
#
# [links.my-skill]
# source = "skills/my-skill"
# target = "~/.claude/skills/"
# description = "Publish this repo's skill to Claude Code"
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


def _print_error(message: str) -> None:
    """Print ``spg: <message>`` to stderr with `message` as literal text.

    Error text routinely contains TOML table names like ``[links.foo]``, which
    rich would otherwise parse as console markup and swallow, so the message is
    appended to a `Text` rather than interpolated into a markup string. It is
    printed with soft wrapping so long absolute paths stay on one line and
    remain copy-pasteable.
    """
    _print_labeled(err_console, "spg: ", "red", message)


def _print_warning(message: str) -> None:
    """Print ``warn: <message>`` to stderr with `message` as literal text."""
    _print_labeled(err_console, "warn: ", "yellow", message)


def _print_labeled(target: Console, label: str, style: str, message: str) -> None:
    line = Text()
    line.append(label, style=style)
    line.append(message)
    target.print(line, soft_wrap=True)


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
        return "~" + text[len(home) :]
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
@click.option(
    "--name",
    "name_",
    default=None,
    help="Project name to seed in the new config (defaults to directory name).",
)
@click.option("-C", "--dir", "directory", default=".", show_default=True, help="Project directory.")
def init(name_: str | None, directory: str) -> int:
    """Create a starter spg.toml in the current directory."""
    target_dir = resolve_path(directory)
    if not target_dir.is_dir():
        _print_error(f"{target_dir} is not a directory")
        return 1
    target = target_dir / PROJECT_CONFIG_FILENAME
    if target.exists():
        _print_error(f"{target} already exists")
        return 1
    project_name = name_ or target_dir.name
    target.write_text(STARTER_TEMPLATE.replace("__SPG_NAME__", project_name))
    console.print(f"[bold green]✓[/] Wrote [bold]{_short_path(target)}[/].")
    console.print("Edit it to declare commands, then run [bold cyan]spg install[/].")
    return 0


@cli.command()
@click.option("-C", "--dir", "directory", default=".", show_default=True, help="Project directory.")
@click.option(
    "--force", is_flag=True, help="Overwrite non-spg-managed files in ~/bin with matching names."
)
@click.option(
    "--without",
    "without",
    multiple=True,
    metavar="SELECTOR",
    help="Don't install this command or link (repeatable). "
    "Use NAME, or cmd:NAME / link:NAME when both are declared.",
)
@click.option(
    "-i",
    "--interactive",
    is_flag=True,
    help="Pick what to install from a checklist (requires a terminal).",
)
def install(directory: str, force: bool, without: tuple[str, ...], interactive: bool) -> int:
    """Register the current project and write wrappers to ~/bin."""
    if without and interactive:
        raise click.UsageError("--without and --interactive are mutually exclusive")
    config = _resolve_config(resolve_path(directory))
    registry = Registry.load(registry_path())
    if interactive:
        # A pre-lock read of the stored exclusions, used *only* to pre-fill the
        # checklist. The installer re-reads them inside the registry lock and is
        # the only thing that decides with them. Keyed by name, the way the
        # installer keys its own lookup, so the checklist cannot mark an item
        # "disabled" from an entry the install will not consult.
        entry = registry.projects.get(config.name)
        changes = _interactive_select(
            config,
            excluded_commands=entry.excluded_commands if entry else (),
            excluded_links=entry.excluded_links if entry else (),
        )
    else:
        changes = _disable_change(config, without)
    result = install_project(config, registry, bin_dir(), force=force, changes=changes)
    _print_install_result(result)
    return 0


@cli.command(short_help="Stop installing named commands or links for this project.")
@click.argument("names", nargs=-1, required=True, metavar="SELECTOR...")
@click.option("-C", "--dir", "directory", default=".", show_default=True, help="Project directory.")
def disable(names: tuple[str, ...], directory: str) -> int:
    """Stop installing the named commands or links for this project.

    Removes their wrappers and links now, and records the choice so
    `spg install` and `spg sync` keep honoring it. Name an item as NAME, or as
    cmd:NAME / link:NAME when a command and a link share a name.
    """
    return _apply_exclusion_change(directory, names, enable=False)


@cli.command(short_help="Install named commands or links for this project again.")
@click.argument("names", nargs=-1, required=True, metavar="SELECTOR...")
@click.option("-C", "--dir", "directory", default=".", show_default=True, help="Project directory.")
def enable(names: tuple[str, ...], directory: str) -> int:
    """Install the named commands or links for this project again.

    Undoes an earlier `spg disable` (or `spg install --without`) and recreates
    the wrapper or link immediately.
    """
    return _apply_exclusion_change(directory, names, enable=True)


def _apply_exclusion_change(directory: str, names: tuple[str, ...], *, enable: bool) -> int:
    config = _resolve_config(resolve_path(directory))
    registry = Registry.load(registry_path())
    changes = (
        _enable_change(config, names, registry.projects.get(config.name))
        if enable
        else _disable_change(config, names)
    )
    result = sync_project(config, registry, bin_dir(), changes=changes)
    _print_install_result(result, verb="Updated")
    return 0


@cli.command()
@click.argument("name", required=False)
@click.option(
    "-C",
    "--dir",
    "directory",
    default=".",
    show_default=True,
    help="Project directory (used when no name is given).",
)
def uninstall(name: str | None, directory: str) -> int:
    """Remove wrappers and registry entry for a project."""
    registry = Registry.load(registry_path())
    project_name = name
    if project_name is None:
        config = _resolve_config(resolve_path(directory))
        project_name = config.name
    result = uninstall_project(project_name, registry, bin_dir())
    console.print(f"[bold green]✓[/] Uninstalled [bold cyan]{result.project}[/].")
    if result.removed:
        _print_change_line("-", "red", "removed", result.removed)
    if result.links_removed:
        _print_change_line("-", "red", "unlinked", result.links_removed)
    if result.skipped:
        _print_change_line("!", "yellow", "skipped", result.skipped)
        console.print("  [dim](skipped entries are not spg-managed)[/]")
    if result.links_skipped:
        _print_change_line("!", "yellow", "kept links", result.links_skipped)
        console.print("  [dim](kept links are no longer symlinks; left untouched)[/]")
    return 0


@cli.command(short_help="Refresh wrappers and links from every registered spg.toml.")
def sync() -> int:
    """Refresh ~/bin wrappers and links from every registered spg.toml, and prune orphans."""
    registry = Registry.load(registry_path())
    if not registry.projects:
        console.print("[dim]No projects registered.[/] Run [bold cyan]spg install[/] in a project.")
        return 0
    any_failures = False
    failed_projects: set[str] = set()
    for entry in list(registry):
        config_file = entry.root / PROJECT_CONFIG_FILENAME
        if not config_file.is_file():
            _print_warning(f"{entry.name}: {config_file} is missing; skipping")
            any_failures = True
            failed_projects.add(entry.name)
            continue
        try:
            config = load_project_config(config_file)
        except ConfigError as exc:
            _print_warning(f"{entry.name}: {exc}")
            any_failures = True
            failed_projects.add(entry.name)
            continue
        try:
            result = sync_project(config, registry, bin_dir())
        except InstallError as exc:
            _print_warning(f"{entry.name}: {exc}")
            any_failures = True
            failed_projects.add(entry.name)
            continue
        _print_install_result(result, verb="Synced")
    prune_result = prune_orphan_wrappers(registry, bin_dir(), skip_projects=failed_projects)
    if prune_result.removed:
        console.print("[bold green]✓[/] Pruned orphaned wrappers.")
        _print_change_line("-", "red", "removed", prune_result.removed)
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
    total_links = sum(len(e.links) for e in entries)
    any_excluded = any(e.has_exclusions for e in entries)

    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False, pad_edge=False)
    table.add_column("Project", style="bold cyan", no_wrap=True)
    table.add_column("Commands", style="green")
    if total_links:
        table.add_column("Links", style="magenta")
    if any_excluded:
        table.add_column("Disabled", style="dim")
    table.add_column("Root", style="dim", overflow="fold")
    for entry in entries:
        commands = (
            Text("  ").join(Text(c, style="green") for c in entry.commands)
            if entry.commands
            else Text("(none)", style="dim italic")
        )
        row: list[RenderableType] = [entry.name, commands]
        if total_links:
            row.append(
                Text("  ").join(Text(link.name, style="magenta") for link in entry.links)
                if entry.links
                else Text("(none)", style="dim italic")
            )
        if any_excluded:
            excluded = entry_excluded_selectors(entry)
            row.append(
                Text("  ").join(Text(selector, style="dim") for selector in excluded)
                if excluded
                else Text("—", style="dim")
            )
        row.append(_short_path(entry.root))
        table.add_row(*row)

    console.print(table)
    project_word = "project" if len(entries) == 1 else "projects"
    command_word = "command" if total_commands == 1 else "commands"
    tally = f"{len(entries)} {project_word}, {total_commands} {command_word}"
    if total_links:
        tally += f", {total_links} {'link' if total_links == 1 else 'links'}"
    console.print(f"[dim]{tally}.[/]")
    return 0


@cli.command(name="help", short_help="List commands exposed via spg, or show usage for one.")
@click.argument("name", required=False)
def help_(name: str | None) -> int:
    """List the commands exposed via spg, or show usage for one of them.

    With no NAME, prints every registered command grouped by the project that
    owns it. With a NAME, prints that command's declared description, arguments,
    and what it runs.
    """
    registry = Registry.load(registry_path())
    if name is None:
        return _print_help_overview(registry)
    owner = registry.find_owner_of_command(name)
    if owner is None:
        _print_error(f"no registered command named {name!r}")
        suggestions = get_close_matches(name, list_managed_commands(registry), n=3, cutoff=0.7)
        if suggestions:
            _print_labeled(err_console, "did you mean: ", "yellow", ", ".join(suggestions))
        err_console.print("Run [bold cyan]spg help[/] to list available commands.")
        return 1
    config_file = owner.root / PROJECT_CONFIG_FILENAME
    if not config_file.is_file():
        _print_error(f"{owner.name}: {config_file} is missing")
        return 1
    config = load_project_config(config_file)
    cmd = config.command(name)
    if cmd is None:
        _print_error(f"registry says {owner.name} owns {name}, but it's no longer in {config_file}")
        err_console.print("Run [bold cyan]spg sync[/] to refresh the registry.")
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
    console.print(
        Panel(Group(*body), title=title, title_align="left", box=box.ROUNDED, padding=(1, 2))
    )
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
    total_links = sum(len(e.links) for e in registered.values())
    if total_links:
        summary.add_row("Managed links", str(total_links))
    console.print(summary)

    by_project: dict[str, list[str]] = {}
    for path, meta in wrappers:
        by_project.setdefault(meta.project, []).append(path.name)

    problems: list[str] = []
    # Declined items the project no longer declares. Informational only: per the
    # ADR a stale exclusion is kept, never pruned and never a failure.
    notes: list[str] = []
    shell_fn_commands: dict[str, set[str]] = {}

    for name, entry in registered.items():
        config_file = entry.root / PROJECT_CONFIG_FILENAME
        try:
            declared: ProjectConfig | None = load_project_config(config_file)
        except (ConfigError, OSError):
            declared = None
        # Everything below works from the config minus this user's declined
        # items: those are *expected* to be absent, so their absence is not drift.
        project_config = (
            declared.without(commands=entry.excluded_commands, links=entry.excluded_links)
            if declared is not None
            else None
        )
        if declared is not None:
            notes.extend(_stale_exclusion_notes(name, entry, declared))
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

        problems.extend(_link_problems(name, entry, project_config))

    for project, found in by_project.items():
        if project not in registered:
            problems.append(
                f"orphan wrappers from unregistered project {project!r}: {', '.join(found)} "
                "(run `spg sync` to prune)"
            )
            continue
        expected = set(registered[project].commands)
        for cmd in found:
            if cmd not in expected:
                problems.append(
                    f"wrapper {cmd!r} not declared in {project!r}'s spg.toml "
                    "(run `spg sync` to prune)"
                )

    if notes:
        console.print()
        for note in notes:
            line = Text()
            line.append("note: ", style="dim bold")
            line.append(note, style="dim")
            console.print(line, soft_wrap=True)

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
            Panel(
                items,
                title=f"[bold red]{count}[/]",
                title_align="left",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        return 1
    console.print("\n[bold green]✓[/] All good.")
    return 0


def _stale_exclusion_notes(
    project: str,
    entry: RegistryEntry,
    declared: ProjectConfig,
) -> list[str]:
    """Declined names `declared` no longer has, reported without failing status."""
    notes: list[str] = []
    stale: list[tuple[SelectorKind, str]] = [
        ("command", n) for n in entry.excluded_commands if declared.command(n) is None
    ]
    stale += [("link", n) for n in entry.excluded_links if declared.link(n) is None]
    for kind, name in stale:
        selector = display_selector(kind, name)
        notes.append(
            f"{project}: disabled {selector} is no longer declared in "
            f"{PROJECT_CONFIG_FILENAME}; kept in case it comes back "
            f"(run `spg enable {selector}` to forget it)"
        )
    return notes


@cli.group(
    invoke_without_command=True,
    context_settings=CONTEXT_SETTINGS,
    short_help="Print a shell completion script.",
)
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
    except (ConfigError, InstallError, RegistryError) as exc:
        _print_error(str(exc))
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


def _resolve_selectors(
    config: ProjectConfig,
    selectors: Collection[str],
    *,
    also_commands: Collection[str] = (),
    also_links: Collection[str] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve typed selectors into (command names, link names).

    Raises `ConfigError` on the first bad one — freshly typed selectors are
    rejected loudly, unlike stored exclusions.
    """
    commands: list[str] = []
    links: list[str] = []
    for selector in selectors:
        kind, name = resolve_selector(
            config, selector, also_commands=also_commands, also_links=also_links
        )
        target = commands if kind == "command" else links
        if name not in target:
            target.append(name)
    return tuple(commands), tuple(links)


def _disable_change(config: ProjectConfig, selectors: Collection[str]) -> ExclusionChange:
    commands, links = _resolve_selectors(config, selectors)
    return ExclusionChange(disable_commands=commands, disable_links=links)


def _enable_change(
    config: ProjectConfig,
    selectors: Collection[str],
    entry: RegistryEntry | None,
) -> ExclusionChange:
    """Build an enable request, accepting names the project no longer declares.

    A stored exclusion outlives the declaration it names (per the ADR it is kept,
    not pruned), and `spg status` reports it, so `spg enable` has to be able to
    name it — otherwise there is no way to clear one at all.

    The pre-lock read of `entry` only widens *what parses*; the effective
    exclusion set is still computed from the entry the installer reads inside the
    lock, where enabling a name that is no longer stored is simply a no-op.
    """
    commands, links = _resolve_selectors(
        config,
        selectors,
        also_commands=entry.excluded_commands if entry else (),
        also_links=entry.excluded_links if entry else (),
    )
    return ExclusionChange(enable_commands=commands, enable_links=links)


def _interactive_select(
    config: ProjectConfig,
    *,
    excluded_commands: Collection[str],
    excluded_links: Collection[str],
) -> ExclusionChange:
    """Ask which declared items to skip, and turn the answer into a change.

    The answer is authoritative rather than additive over the items on the list:
    anything declared that the user does not skip is (re-)enabled, so re-running
    `spg install -i` fully describes the desired state. Currently-declined items
    are marked in the table and listed above the prompt, since an empty answer
    means "install everything".
    """
    if not sys.stdin.isatty():
        raise click.UsageError("--interactive requires a terminal; stdin is not a TTY")

    items: list[tuple[SelectorKind, str]] = [("command", c.name) for c in config.commands] + [
        ("link", link.name) for link in config.links
    ]
    if not items:
        console.print(
            f"[dim]{config.name} declares no commands or links to choose from.[/]",
        )
        return ExclusionChange()

    already: set[tuple[SelectorKind, str]] = {("command", n) for n in excluded_commands} | {
        ("link", n) for n in excluded_links
    }

    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False, pad_edge=False)
    table.add_column("#", style="bold", justify="right", no_wrap=True)
    table.add_column("Item", style="green", no_wrap=True)
    table.add_column("Description", overflow="fold")
    table.add_column("", style="dim", no_wrap=True)
    for number, item in enumerate(items, start=1):
        kind, name = item
        if kind == "command":
            cmd = config.command(name)
            detail = Text(cmd.description if cmd and cmd.description else "", style="italic")
            label = Text(name, style="green")
        else:
            link = config.link(name)
            detail = Text(link.description if link and link.description else "", style="italic")
            if link is not None:
                if detail.plain:
                    detail.append("  ")
                detail.append(f"→ {_short_path(link.link_path)}", style="dim")
            label = Text(name, style="magenta")
        table.add_row(str(number), label, detail, "disabled" if item in already else "")

    console.print()
    console.print(f"[bold]{config.name}[/] declares:")
    console.print(table)
    marked = [str(i) for i, item in enumerate(items, start=1) if item in already]
    if marked:
        console.print(
            f"[dim]Currently disabled: {', '.join(marked)} — "
            "list them again to keep them disabled.[/]"
        )

    while True:
        answer = click.prompt(
            "Numbers to skip (comma-separated, empty = install everything)",
            default="",
            show_default=False,
        )
        try:
            skipped = _parse_number_list(answer, len(items))
        except ValueError as exc:
            _print_error(str(exc))
            continue
        break

    disable = [items[i - 1] for i in skipped]
    # Authoritative only over what the checklist actually showed. A stored
    # exclusion for a name this spg.toml no longer declares is not on the list,
    # so the user cannot have answered about it: per the ADR it is kept, not
    # pruned. `spg enable <name>` is the explicit way to clear one.
    shown = set(items)
    enable = [item for item in already if item in shown and item not in disable]
    return ExclusionChange(
        disable_commands=tuple(name for kind, name in disable if kind == "command"),
        disable_links=tuple(name for kind, name in disable if kind == "link"),
        enable_commands=tuple(name for kind, name in enable if kind == "command"),
        enable_links=tuple(name for kind, name in enable if kind == "link"),
    )


def _parse_number_list(answer: str, count: int) -> list[int]:
    """Parse a comma/space separated list of 1-based item numbers."""
    numbers: list[int] = []
    for token in answer.replace(",", " ").split():
        try:
            number = int(token)
        except ValueError:
            raise ValueError(
                f"{token!r} is not a number; enter numbers from the list above"
            ) from None
        if not 1 <= number <= count:
            raise ValueError(f"{number} is out of range; pick between 1 and {count}")
        if number not in numbers:
            numbers.append(number)
    return numbers


def _print_help_overview(registry: Registry) -> int:
    """List every registered command, grouped by the project that owns it."""
    entries = sorted(registry, key=lambda e: e.name)
    if not entries:
        console.print("[dim]No projects registered.[/] Run [bold cyan]spg install[/] in a project.")
        return 0
    if not any(entry.commands for entry in entries):
        console.print(
            "[dim]No commands registered.[/] Declare one in a project's "
            f"[bold]{PROJECT_CONFIG_FILENAME}[/] and run [bold cyan]spg install[/]."
        )
        return 0

    table = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=False, pad_edge=False)
    table.add_column("Project", style="bold cyan", no_wrap=True)
    table.add_column("Command", style="green", no_wrap=True)
    table.add_column("Description", overflow="fold")
    first_group = True
    for entry in entries:
        if not entry.commands:
            continue
        if not first_group:
            table.add_section()
        first_group = False
        summaries = _command_summaries(entry)
        for i, cmd_name in enumerate(sorted(entry.commands)):
            table.add_row(entry.name if i == 0 else "", cmd_name, summaries[cmd_name])

    console.print(table)
    console.print("[dim]Run[/] [bold cyan]spg help <command>[/] [dim]for a command's details.[/]")
    return 0


def _command_summaries(entry: RegistryEntry) -> dict[str, Text]:
    """One-line description per registered command name, read from the project's spg.toml.

    Falls back to a dim note when the config can't be read or no longer declares
    a command the registry still records — `spg help` is a discovery entry point,
    so it reports the drift instead of failing on it.
    """
    config_file = entry.root / PROJECT_CONFIG_FILENAME
    try:
        config: ProjectConfig | None = load_project_config(config_file)
    except (ConfigError, OSError):
        config = None

    summaries: dict[str, Text] = {}
    for cmd_name in entry.commands:
        if config is None:
            summaries[cmd_name] = Text(
                f"({PROJECT_CONFIG_FILENAME} is missing or unreadable)", style="dim italic"
            )
            continue
        cmd = config.command(cmd_name)
        if cmd is None:
            summaries[cmd_name] = Text(
                f"(no longer in {PROJECT_CONFIG_FILENAME} — run `spg sync`)", style="dim italic"
            )
            continue
        if cmd.description:
            summary = Text(cmd.description)
        else:
            summary = Text("(no description)", style="dim italic")
        if cmd.is_shell_function:
            summary.append("  shell function", style="dim")
        summaries[cmd_name] = summary
    return summaries


def _link_problems(
    project: str,
    entry: RegistryEntry,
    project_config: ProjectConfig | None,
) -> list[str]:
    """Report drift between a project's declared links and the filesystem.

    `project_config` must already have this user's declined links filtered out
    (see `status`): a declined link is expected to be absent, so reporting it
    would hold `spg status` at exit 1 forever.
    """
    problems: list[str] = []
    registered_paths = {link.path for link in entry.links}

    if project_config is None:
        # Can't read spg.toml, so we don't know the intended sources — only
        # check that what we recorded still looks like a link.
        for link in entry.links:
            if not link.path.is_symlink():
                verb = "is missing" if not link.path.exists() else "is no longer a symlink"
                problems.append(f"link {link.name!r} {verb}: {link.path} (owned by {project})")
        return problems

    for link in project_config.links:
        link_path = link.link_path
        state = link_state(link, project_config.root)
        if state == "missing":
            problems.append(f"missing link: {link_path} (owned by {project})")
        elif state == "foreign":
            problems.append(
                f"link {link_path} points at {link_path.readlink()}, expected "
                f"{link.source_path(project_config.root)} (owned by {project})"
            )
        elif state in ("file", "dir", "other"):
            problems.append(
                f"link path {link_path} is not a symlink (owned by {project}); "
                "spg will not replace it"
            )
        if link_path not in registered_paths:
            problems.append(
                f"link {link.name!r} is declared in {project}'s spg.toml but not registered "
                "(run `spg sync`)"
            )

    declared_paths = {link.link_path for link in project_config.links}
    for link in entry.links:
        if link.path not in declared_paths:
            problems.append(
                f"stale link {link.name!r} ({link.path}) is no longer declared in {project}'s "
                "spg.toml (run `spg sync` to remove it)"
            )
    return problems


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
    if result.links_written:
        _print_change_line("+", "green", "linked", result.links_written)
    if result.links_relinked:
        _print_change_line("~", "yellow", "relinked", result.links_relinked)
    if result.links_removed:
        _print_change_line("-", "red", "unlinked", result.links_removed)
    if result.links_kept:
        _print_change_line("!", "yellow", "kept links", result.links_kept)
        console.print("  [dim](kept links are no longer symlinks; left untouched)[/]")
    # Report what is declined on every install/sync, so a missing command is
    # explained rather than mysterious.
    if result.excluded:
        _print_change_line("⊘", "dim", "disabled", result.excluded)
    if not (
        result.written
        or result.refreshed
        or result.removed
        or result.links_written
        or result.links_relinked
        or result.links_removed
        or result.links_kept
        or result.excluded
    ):
        console.print("  [dim]no changes.[/]")


if __name__ == "__main__":
    raise SystemExit(main())
