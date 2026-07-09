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

`spg` is a Python package managed with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install spg          # install the CLI globally
# or, from a clone:
uv run spg --help            # run without installing
```

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
spg help <cmd>  # show a command's declared usage
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

See [`prompts/add-spg-support.md`](prompts/add-spg-support.md) for the full,
authoritative schema and an agent-ready guide to adding `spg` to any project.

## Commands

| Command | What it does |
| --- | --- |
| `spg init` | Write a starter `spg.toml` in the current directory. |
| `spg install` | Register the project and write `~/bin` wrappers. |
| `spg uninstall [name]` | Remove a project's wrappers and registry entry. |
| `spg sync` | Re-read every registered project's `spg.toml`, refresh wrappers, and prune orphaned ones. |
| `spg list` | Show registered projects and their commands. |
| `spg help <cmd>` | Show a published command's declared usage. |
| `spg status` | Diagnose registry / `~/bin` drift. |
| `spg completion zsh` | Print the zsh completion script. |

## How it works

- The registry lives at `~/.config/spg/registry.toml` (respects
  `XDG_CONFIG_HOME`). It records each project's root and its commands.
- Wrappers are written to `~/bin/<cmd>`. Each carries a
  `# spg-managed:<project>:<command>` marker so `spg` knows which files it owns
  — don't edit them by hand. Wrapper and registry writes are atomic, and the
  registry is updated under an exclusive file lock so concurrent `spg`
  invocations can't corrupt it.
- `spg install` refuses to clobber a non-spg file or another project's command;
  pass `--force` only to overwrite a non-spg `~/bin/<cmd>`.
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
