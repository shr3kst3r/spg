from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from spg.config import (
    Command,
    CommandArg,
    ConfigError,
    ProjectConfig,
    display_selector,
    load_project_config,
)
from spg.paths import PROJECT_CONFIG_FILENAME, find_project_config, invocation_dir
from spg.registry import Registry

SENTINEL_FILES = "__files__"
SENTINEL_DIRS = "__directories__"

# Subcommand names and descriptions offered when completing `spg <TAB>`.
# Duplicated here on purpose: this module sits below `cli` in the dependency
# chain and must not import it. `tests/test_completion.py` asserts this table
# matches the CLI's own commands and short help, so the two can't drift.
SPG_SUBCOMMANDS: tuple[tuple[str, str], ...] = (
    ("init", "Create a starter spg.toml in the current directory"),
    ("install", "Register the current project and write wrappers to ~/bin"),
    ("uninstall", "Remove wrappers and registry entry for a project"),
    ("sync", "Refresh wrappers and links from every registered spg.toml"),
    ("disable", "Stop installing named commands or links for this project"),
    ("enable", "Install named commands or links for this project again"),
    ("list", "Show registered projects and their commands"),
    ("help", "List commands exposed via spg, or show usage for one"),
    ("status", "Diagnose registry / ~/bin mismatches"),
    ("completion", "Print a shell completion script"),
)


def list_managed_commands(registry: Registry) -> list[str]:
    out: list[str] = []
    for entry in registry:
        out.extend(entry.commands)
    return sorted(set(out))


def render_shell_function_defs(registry: Registry) -> str:
    """Return zsh-syntax function definitions for every registered shell_function command.

    Parses each project's spg.toml on demand; silently skips projects whose
    config is missing or malformed (better than blocking shell startup).

    Reads the config directly rather than deriving from the registry entry (it
    needs the function bodies, which the registry does not store), so it has to
    filter the entry's declined commands itself — otherwise a declined
    shell_function would still be sourced into the user's shell.
    """
    chunks: list[str] = []
    for entry in sorted(registry, key=lambda e: e.name):
        config_file = entry.root / PROJECT_CONFIG_FILENAME
        try:
            project_config = load_project_config(config_file)
        except (ConfigError, OSError):
            continue
        for cmd in project_config.without(commands=entry.excluded_commands).commands:
            if not cmd.is_shell_function:
                continue
            chunks.append(f"{cmd.name}() {{\n{cmd.shell_function}\n}}\n")
    return "".join(chunks)


def candidates_for_spg(words: list[str], current: int, registry: Registry) -> list[str]:
    """Candidates when completing `spg ...`. `current` is 1-indexed, `words[0]` is 'spg'."""
    if current <= 1:
        return []
    if current == 2:
        return [f"{n}:{d}" for n, d in SPG_SUBCOMMANDS]

    sub = words[1] if len(words) > 1 else ""
    prev = words[current - 2] if current - 2 >= 0 and current - 2 < len(words) else ""

    if prev in ("-C", "--dir"):
        return [SENTINEL_DIRS]
    if prev == "--name":
        return []

    if sub == "help" and current == 3:
        return list_managed_commands(registry)
    if sub == "uninstall" and current == 3:
        return sorted(registry.projects.keys())
    if sub == "completion" and current == 3:
        return ["zsh:Print a zsh completion script"]
    # Both take NAME... , so keep offering selectors past the first one.
    if sub in ("disable", "enable") and current >= 3:
        return selector_candidates(registry, excluded=sub == "enable")

    return []


def selector_candidates(registry: Registry, *, excluded: bool) -> list[str]:
    """Selectors for the project at the CWD, as completion candidates.

    With `excluded=False` (for `spg disable`) offers everything declared that is
    not already declined; with `excluded=True` (for `spg enable`) offers what the
    registry entry records as declined — including a name this spg.toml no longer
    declares, since clearing one of those is something only `spg enable` can do.
    Never raises — a completion helper that raises breaks the user's shell, so
    any failure to find or read a config yields no candidates.
    """
    try:
        config_file = find_project_config(invocation_dir())
        if config_file is None:
            return []
        config = load_project_config(config_file)
        # Keyed by name, matching how `spg disable`/`spg enable` and the
        # installer look the project up.
        entry = registry.projects.get(config.name)
    except Exception:
        return []

    stored_commands = entry.excluded_commands if entry else ()
    stored_links = entry.excluded_links if entry else ()
    if excluded:
        command_names: tuple[str, ...] = stored_commands
        link_names: tuple[str, ...] = stored_links
    else:
        command_names = tuple(c.name for c in config.commands if c.name not in stored_commands)
        link_names = tuple(link.name for link in config.links if link.name not in stored_links)

    # Prefix a selector only when the bare name is ambiguous in the namespaces
    # the command will resolve against, so the common case stays short.
    known_commands = {c.name for c in config.commands}
    known_links = {link.name for link in config.links}
    if excluded:
        known_commands |= set(stored_commands)
        known_links |= set(stored_links)
    collisions = known_commands & known_links

    out: list[str] = []
    for name in command_names:
        value = display_selector("command", name) if name in collisions else name
        out.append(_describe(value, _command_description(config, name)))
    for name in link_names:
        value = display_selector("link", name) if name in collisions else name
        out.append(_describe(value, _link_description(config, name)))
    return out


_STALE_DESCRIPTION = f"(no longer declared in {PROJECT_CONFIG_FILENAME})"


def _command_description(config: ProjectConfig, name: str) -> str:
    cmd = config.command(name)
    if cmd is None:
        return _STALE_DESCRIPTION
    return cmd.description or "(no description)"


def _link_description(config: ProjectConfig, name: str) -> str:
    link = config.link(name)
    if link is None:
        return _STALE_DESCRIPTION
    return link.description or f"link → {link.link_path}"


def _describe(value: str, description: str) -> str:
    """One `value:description` completion line, escaping colons inside the value.

    zsh's `_describe` splits each line on its first unescaped colon, so a
    prefixed selector like `cmd:foo` has to arrive as `cmd\\:foo`.
    """
    escaped = value.replace(":", r"\:")
    return f"{escaped}:{description}" if description else escaped


def candidates_for_command(
    name: str,
    words: list[str],
    current: int,
    registry: Registry,
) -> list[str]:
    """Candidates when completing a managed command `<name> ...`."""
    if name == "spg":
        return candidates_for_spg(words, current, registry)

    entry = registry.find_owner_of_command(name)
    if entry is None:
        return []
    try:
        config = load_project_config(entry.root / PROJECT_CONFIG_FILENAME)
    except (ConfigError, OSError):
        return []
    cmd = config.command(name)
    if cmd is None:
        return []

    cur_word = words[current - 1] if 0 <= current - 1 < len(words) else ""
    prev_word = words[current - 2] if 0 <= current - 2 < len(words) else ""

    flags = {a.name: a for a in cmd.args if a.is_flag}
    positionals = [a for a in cmd.args if not a.is_flag]

    if prev_word in flags:
        flag = flags[prev_word]
        if flag.values:
            return _value_candidates(flag.values, flag.description or flag.name)
        if flag.type == "files":
            return [SENTINEL_FILES]
        if flag.type == "directories":
            return [SENTINEL_DIRS]
        # boolean flag — fall through to positional logic

    if cur_word.startswith("-"):
        static_flags = [f"{f.name}:{f.description}" for f in flags.values()]
        if static_flags:
            return static_flags
        # No static flags declared — let the hook supply candidates if it can.
        if cmd.complete_hook:
            return _run_hook(cmd, entry.root, words, current)
        return []

    pos_index = _positional_index(words, current, flags)
    if 0 <= pos_index < len(positionals):
        arg = positionals[pos_index]
        if arg.values:
            return _value_candidates(arg.values, arg.description or arg.name)
        if arg.type == "files":
            return [SENTINEL_FILES]
        if arg.type == "directories":
            return [SENTINEL_DIRS]

    if cmd.complete_hook:
        return _run_hook(cmd, entry.root, words, current)

    return []


def _value_candidates(values: tuple[str, ...], description: str) -> list[str]:
    if description:
        return [f"{v}:{description}" for v in values]
    return list(values)


def _positional_index(
    words: list[str],
    current: int,
    flags: dict[str, CommandArg],
) -> int:
    """Count filled positionals before `current` (1-indexed), skipping flags."""
    i = 1  # words[0] is the command name itself
    filled = 0
    while i < current - 1:
        if i >= len(words):
            break
        w = words[i]
        if w in flags:
            if flags[w].expects_value:
                i += 2
            else:
                i += 1
        elif w.startswith("-"):
            i += 1
        else:
            filled += 1
            i += 1
    return filled


def _run_hook(cmd: Command, root: Path, words: list[str], current: int) -> list[str]:
    try:
        argv = shlex.split(cmd.complete_hook)
    except ValueError:
        return []
    if not argv:
        return []
    argv.append(str(current - 1))
    argv.extend(words[1:])
    try:
        result = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


ZSH_SCRIPT = r"""#compdef spg
# spg completion — generated by `spg completion zsh`.
# Install: add `source <(spg completion zsh)` to your .zshrc.
#
# This script dispatches into `spg __complete` at completion time, so
# updates to ~/.config/spg/registry.toml or any project's spg.toml are
# reflected automatically on the next shell.

_spg_handle() {
    local cmpl_out
    cmpl_out="$(spg __complete "$@" 2>/dev/null)"
    case "$cmpl_out" in
        __files__*)        _files; return ;;
        __directories__*)  _path_files -/; return ;;
    esac
    local -a candidates
    candidates=("${(@f)cmpl_out}")
    if (( ${#candidates} )); then
        _describe -t values "${1:-completion}" candidates
    fi
}

_spg_dispatch() {
    _spg_handle spg "$CURRENT" "${words[@]}"
}

_spg_cmd_dispatch() {
    _spg_handle cmd "${words[1]}" "$CURRENT" "${words[@]}"
}

compdef _spg_dispatch spg

local -a _spg_managed
_spg_managed=("${(@f)$(spg __complete list-commands 2>/dev/null)}")
local _spg_managed_cmd
for _spg_managed_cmd in "${_spg_managed[@]}"; do
    [[ -z "$_spg_managed_cmd" ]] && continue
    [[ "$_spg_managed_cmd" == spg ]] && continue
    compdef _spg_cmd_dispatch "$_spg_managed_cmd"
done

# Shell-function commands need to run in the current shell (they cd, set vars,
# etc.), so spg emits their bodies here and we eval them into the live shell.
source <(spg __complete list-shell-functions 2>/dev/null)
"""


def render_zsh_completion() -> str:
    return ZSH_SCRIPT


__all__ = [
    "SENTINEL_DIRS",
    "SENTINEL_FILES",
    "SPG_SUBCOMMANDS",
    "ZSH_SCRIPT",
    "candidates_for_command",
    "candidates_for_spg",
    "list_managed_commands",
    "render_shell_function_defs",
    "render_zsh_completion",
    "selector_candidates",
]
