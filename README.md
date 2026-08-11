# spg

A tiny per-project command publisher. `spg` reads a project's `spg.toml` and
exposes each declared command on your machine — either as a wrapper script in
`~/bin` or as a function defined in your interactive shell — with rich zsh tab
completion driven by the same declarations.

Think of it as a portable, per-project alternative to hand-rolled `~/bin`
scripts and `.zshrc` aliases: the commands live with the project, version
controlled, and `spg` materializes them onto your `$PATH` (or into your shell).

## Why

Most projects accumulate a handful of commands you actually want on your
`$PATH` — a deploy script, a dev-server launcher, a `cd`-into-the-worktree
helper. `spg` lets a project declare those once, in `spg.toml`, and publish
them with a single `spg install`. The declarations also drive tab completion,
so `mycmd <TAB>` offers the right arguments without any extra wiring.

## Install

`spg` is a Python package managed with [uv](https://docs.astral.sh/uv/). It is
not on PyPI (the `spg` name there belongs to an unrelated project), so install
it straight from the repository:

```sh
uv tool install git+https://github.com/shr3kst3r/spg.git   # install the CLI globally
# or, from a clone:
uv tool install .            # install the CLI from your working copy
uv run spg --help            # run without installing
```

To pick up new commits later, re-run the install with `--force`, or
`uv tool upgrade spg`.

Add completion (zsh) to your `~/.zshrc`:

```sh
source <(spg completion zsh)
```

This both enables tab completion **and** defines any `shell_function` commands
in your interactive shell. Make sure `~/bin` is on your `$PATH`.

## Quick start

From a project you want to publish commands for:

```sh
spg init        # write a starter spg.toml
$EDITOR spg.toml  # declare your commands
spg install     # write ~/bin/<cmd> wrappers and register the project
spg list        # confirm what's registered
spg help        # list every published command, grouped by project
spg help <cmd>  # show one command's declared usage
```

## `spg.toml`

```toml
[project]
name = "my-project"            # required; [A-Za-z_][A-Za-z0-9_.-]*

# Kind 1 — wrapper script (the default). Becomes ~/bin/deploy.
[commands.deploy]
run = "./scripts/deploy.sh"    # arbitrary shell, run from the repo root
description = "Deploy the app"
args = [
    { name = "target",   description = "environment", values = ["staging", "prod"] },
    { name = "config",   description = "config file", type = "files" },
    { name = "--region", description = "AWS region",  values = ["us-east-1", "eu-west-1"] },
    { name = "--dry-run", description = "no changes" },   # boolean flag
]
complete_hook = "./scripts/deploy.sh __complete"   # optional dynamic completion

# Kind 2 — shell function (runs in the parent shell). For cd/export/etc.
[commands.gocd]
description = "cd into a resolved worktree"
shell_function = 'cd "$(./scripts/resolve.sh "$@")"'

# Symlinks — publish a path in this repo to somewhere on your machine.
[links.my-skill]
source = "skills/my-skill"      # relative to the repo root
target = "~/.claude/skills/"    # trailing slash: link *into* this directory,
                                # leaf name = the table name ("my-skill")
description = "Publish this repo's skill to Claude Code"

[links.rgconf]
source = "config/ripgreprc"
target = "~/.config/ripgrep/config"   # no trailing slash: the exact link path
```

Rules in brief:

- Exactly one of `run` or `shell_function` per command.
- `run` produces a `~/bin/<name>` wrapper that `cd`s into the repo root and
  runs `sh -c '<run> "$@"'` with your arguments. Use it for normal commands.
  Before `cd`ing, the wrapper exports `$SPG_INVOCATION_DIR` — the directory you
  invoked the command from — so a command can act on where you are rather than
  where it's installed (e.g. `some-tool "${SPG_INVOCATION_DIR:-.}"`).
- `shell_function` produces a function defined in your interactive shell (it
  can `cd`, `export`, set shell variables — things a subprocess can't).
  Available only in interactive zsh with the completion script sourced.
- `args` declare positionals (no leading `-`) and flags (leading `-`/`--`).
  `values` = a closed set; `type` = `"files"` or `"directories"`. A flag with
  neither is boolean.
- `complete_hook` supplies dynamic completion candidates (git branches,
  hostnames, etc.) when static `args` can't. It's invoked as
  `<hook> <position> <words…>` and prints one `value[:description]` per line;
  the sentinels `__files__` / `__directories__` request path completion.
- `[links.<name>]` symlinks `source` (a path inside the repo) to `target`. Use
  it to publish repo content that a tool expects to find in a fixed location —
  a Claude Code skill in `~/.claude/skills/`, a dotfile, an editor config.
  `source` must be relative to the repo root and must exist; `target` must be
  absolute (`~` is expanded). A `target` ending in `/` means "link into that
  directory" with `<name>` as the leaf; otherwise `target` is the link itself.
  Missing parent directories are created. Links are created, repaired, and
  removed by `spg install` / `sync` / `uninstall` alongside wrappers.

See [`prompts/add-spg-support.md`](prompts/add-spg-support.md) for the full,
authoritative schema and an agent-ready guide to adding `spg` to any project.

## Commands

| Command | What it does |
| --- | --- |
| `spg init` | Write a starter `spg.toml` in the current directory. |
| `spg install` | Register the project, write `~/bin` wrappers, and create declared links. |
| `spg uninstall [name]` | Remove a project's wrappers, links, and registry entry. |
| `spg sync` | Re-read every registered project's `spg.toml`, refresh wrappers and links, and prune orphaned ones. |
| `spg disable <sel>…` | Stop installing specific commands or links for this project (removes them now). |
| `spg enable <sel>…` | Install previously disabled commands or links again. |
| `spg list` | Show registered projects with their commands, links, and anything disabled. |
| `spg help [cmd]` | List every published command grouped by project, or show one command's declared usage. |
| `spg status` | Diagnose registry / `~/bin` / link drift. |
| `spg completion zsh` | Print the zsh completion script. |

## Choosing what gets installed

Everything a project declares is installed by default. If you don't want a
particular command or link on *your* machine, decline it — the choice is yours
alone and lives on your machine, not in the project's `spg.toml`:

```sh
spg install --without deploy --without link:my-skill   # decline at install time
spg install -i                                         # pick from a checklist
spg disable deploy                                     # change your mind later
spg enable deploy                                      # and change it back
```

A **selector** names one declarable item: `<name>`, or `cmd:<name>` /
`link:<name>` when a command and a link share a name. `--without` is repeatable
and mutually exclusive with `-i`; `disable`/`enable` take one or more selectors
and take effect immediately (the wrapper or symlink is removed or recreated).

Declined items are remembered, so `spg sync` won't put them back. `spg list`
shows them in a `Disabled` column, every install and sync prints a
`⊘ disabled` line, and `spg status` stays quiet about them — their absence is
expected, not drift. Declining is also the escape hatch when a project's command
name collides with something you already own in `~/bin`.

If a project later stops declaring something you declined, the choice is kept in
case it comes back, and `spg status` mentions it as a note (never as a problem).
Run `spg enable <sel>` on it to forget the choice for good.

Note that `spg uninstall` forgets a project's declined items along with the rest
of its registry entry, so uninstall/reinstall is not a way to repair an install.

## How it works

- The registry lives at `~/.config/spg/registry.toml` (respects
  `XDG_CONFIG_HOME`). It records each project's root, its commands, and the
  links it published, plus a `version` marking the file's format.
- It also records the commands and links *you* declined for that project
  (`excluded_commands` / `excluded_links`), which is why they are absent. Every
  `spg install` and `spg sync` honors them, and only spg writes that file — you
  change what's declined with `--without`, `-i`, `disable`, and `enable`.
- Wrappers are written to `~/bin/<cmd>`. Each carries a
  `# spg-managed:<project>:<command>` marker so `spg` knows which files it owns
  — don't edit them by hand. Wrapper and registry writes are atomic, and the
  registry is updated under an exclusive file lock so concurrent `spg`
  invocations can't corrupt it.
- `spg install` refuses to clobber a non-spg file or another project's command;
  pass `--force` only to overwrite a non-spg `~/bin/<cmd>`.
- Links get the same treatment: spg only ever removes a path it recorded *and*
  that is still a symlink, so it can't delete your files. A foreign symlink or
  regular file at a link's path needs `--force`; a real directory there is
  refused outright, even with `--force`.
- Completion dispatches into a hidden `spg __complete` command at completion
  time, so registry/`spg.toml` edits show up in your next shell automatically.

## Development

This project self-hosts: its own commands are declared in [`spg.toml`](spg.toml).

```sh
uv run pytest        # run the test suite
uv run ty check      # type-check src/
uv run spg --help    # run the CLI from source
```

Source lives in `src/spg/`; tests in `tests/`. See [AGENTS.md](AGENTS.md) for
the architecture map and contributor conventions.
