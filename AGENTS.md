# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository.

## What this project is

`spg` is a tiny per-project command publisher. It reads a project's `spg.toml`
and exposes each declared command on the user's machine — as a `~/bin/<cmd>`
wrapper script, or as a shell function defined in their interactive shell —
with zsh tab completion generated from the same declarations. It also
materializes declared `[links.<name>]` symlinks, for repo content a tool
expects at a fixed path (e.g. a skill in `~/.claude/skills/`). See
[README.md](README.md) for the user-facing overview and
[`prompts/add-spg-support.md`](prompts/add-spg-support.md) for the authoritative
`spg.toml` schema.

It self-hosts: the project's own commands are declared in [`spg.toml`](spg.toml).

## Tech stack

- Python ≥ 3.11, packaged and run with [uv](https://docs.astral.sh/uv/).
- CLI built on [rich-click](https://github.com/ewels/rich-click) (Click).
- Standard library only at runtime otherwise (`tomllib`, `fcntl`, `tempfile`).
- Tests: `pytest`. Type checking: `ty`.

## Commands

Always run via `uv` so the right environment is used:

```sh
uv run pytest          # full test suite (tests/)
uv run ty check        # type-check src/ (config in pyproject.toml [tool.ty])
uv run ruff check .    # lint (config in pyproject.toml [tool.ruff])
uv run ruff format .   # format
uv run spg --help      # exercise the CLI from source
```

`pytest`, `ty check`, and `ruff` (lint + format) are the gates; all must pass
before you consider a change done. The same checks run in
[`.pre-commit-config.yaml`](.pre-commit-config.yaml) — install the hooks once
with `uv run --with pre-commit pre-commit install`, or run them across the tree
with `uv run --with pre-commit pre-commit run --all-files`.

## Architecture map

All source is in `src/spg/`. The modules form a clean dependency chain — keep
it that way (no upward imports):

| Module | Responsibility |
| --- | --- |
| `paths.py` | Filesystem locations: `~/bin`, registry path (`XDG_CONFIG_HOME`), `spg.toml` discovery by walking up from CWD. No dependencies. |
| `config.py` | Parse & validate `spg.toml` into frozen `ProjectConfig` / `Command` / `CommandArg` / `Link` dataclasses. Raises `ConfigError`. All validation lives here. |
| `registry.py` | Read/write `~/.config/spg/registry.toml` (commands + published links, stamped with `REGISTRY_VERSION`). Atomic writes + an exclusive `flock` transaction (`Registry.locked()`). Hand-rolled TOML serialization. |
| `installer.py` | Materialize/remove `~/bin` wrappers and `[links]` symlinks, detect conflicts, keep the registry in sync. Wrappers carry a `# spg-managed:<project>:<command>` marker. Raises `InstallError`. |
| `completion.py` | Generate zsh completion + compute completion candidates for `spg __complete`. Runs per-command `complete_hook`s (2s timeout). |
| `cli.py` | rich-click command group wiring the above together. Thin — logic belongs in the modules above. |

Data flow: `cli` → `config`/`registry`/`installer`/`completion` → `paths`.

## Conventions

- **`from __future__ import annotations`** at the top of every module.
- **Frozen dataclasses** for parsed/value types (`Command`, `CommandArg`,
  `ProjectConfig`, `WrapperMeta`). Prefer immutability.
- **Validate in `config.py`.** New `spg.toml` fields get parsed and fully
  validated there, with a precise `ConfigError` message that names the
  offending `[commands.<name>]` table. Mirror the existing message style.
- **Atomicity & safety are load-bearing.** Wrapper and registry writes go
  through `tempfile.mkstemp` + `os.replace` (never open an existing dirent for
  writing — a `~/bin/<cmd>` may be a symlink). Registry mutations happen inside
  `Registry.locked()`. Don't regress this.
- **Never clobber files spg doesn't own.** Conflict detection in
  `installer._check_conflicts` / `_link_conflicts` is deliberate; `--force`
  only overrides a non-spg regular file or a foreign symlink, never another
  project's command or link. A real directory in a link's way is refused even
  with `--force`, and removal only ever unlinks a recorded path that is *still
  a symlink* — spg must not be able to delete a user's data.
- **Link identity is the link path**, recorded in the registry (with its
  declaration name for reporting). There's no in-file marker to read back, so
  ownership comes from the registry plus the symlink's current value.
- **CLI commands return an int exit code** and print errors to stderr with an
  `spg: ` prefix. `main()` maps `ConfigError`/`InstallError` to exit 1. Route
  dynamic error text through `_print_error`/`_print_warning`, not an f-string
  into `err_console.print` — messages contain `[commands.x]`/`[links.x]` table
  names that rich would parse as markup and silently swallow.
- Type hints everywhere; `ty check` must stay green.

## Testing

- Tests live in `tests/`, one file per module (`test_config.py`,
  `test_installer.py`, etc.).
- Use the fixtures in `tests/conftest.py`: `make_project` (builds a temp
  project with a `spg.toml`), `bin_dir`, and `registry_file`. They keep tests
  hermetic — never touch the real `~/bin` or `~/.config`.
- Add or update tests alongside any behavior change. Validation changes should
  assert on the specific `ConfigError`/`InstallError` raised.

## Boundaries

- Don't edit `uv.lock` by hand — let `uv` manage it.
- Don't write outside the repo during tests; honor the fixtures' temp paths.
- Don't push, merge, or modify CI unless explicitly asked.
- When the task is ready for review, open a PR via `gw pr open`.
