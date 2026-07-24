# Add `spg.toml` and fully support `spg` in this project

You are adding support for [`spg`](https://github.com/shr3kst3r/spg) to the
current project. `spg` is a tiny per-project command publisher: it reads a
project's `spg.toml` and exposes each declared command either as a wrapper
script in `~/bin` (the default) or as a function defined in the user's
interactive shell, with rich zsh tab completion driven by the same
declarations.

Your job has three parts:

1. **Survey** the project to identify the commands worth publishing.
2. **Author `spg.toml`** at the repo root with `[project]`, one
   `[commands.<name>]` table per command, and a `[links.<name>]` table for any
   repo content that belongs at a fixed path on the machine (skills, configs).
3. **Wire full support** — make sure the commands actually run end-to-end from
   `~/bin`, give them rich completion (`args` with `values`/`type`, or a
   dynamic `complete_hook`), and verify with `spg install` + `spg help`.

Do not push, merge, or modify CI. Only touch `spg.toml` and (when needed)
project scripts that implement `complete_hook` callbacks.

---

## What spg does (short version)

- `spg install` reads `./spg.toml` and records the project in
  `~/.config/spg/registry.toml`. For each `[commands.<cmd>]` table it either:
  - writes a `~/bin/<cmd>` wrapper script (when `run` is set), or
  - registers it as a **shell function** to be defined eagerly in the user's
    interactive shell (when `shell_function` is set).
- It also creates a symlink for each optional `[links.<name>]` table, for repo
  content that a tool expects to find at a fixed path on the machine.
- Each wrapper `cd`s into the project root and runs:
  `sh -c '<run> "$@"' spg <user-args>`. So `run` is plain shell — it can be a
  script path, `make <target>`, `uv run …`, `npm run …`, whatever.
- Shell-function commands are emitted by `spg __complete list-shell-functions`
  and sourced into the live shell by the completion script (which the user
  installs once via `source <(spg completion zsh)` in `~/.zshrc`). Use this
  kind when the command must affect the parent shell — `cd`, `export`,
  setting shell variables, etc. — since a `~/bin` subprocess can't.
- Tab completion is generated from `args` plus an optional `complete_hook`
  per command. The hook is a shell command that prints one candidate per line
  (`value:description` accepted) when invoked as:
  `<hook> <position> <words…>`  (position is 1-indexed against args after the
  command name; `words[0]` is the command itself).
- `spg sync` re-reads every registered project's `spg.toml`; `spg help <cmd>`
  shows declared usage; `spg status` reports drift.

---

## `spg.toml` schema (authoritative)

```toml
[project]
name = "my-project"           # required; non-empty string

# One [commands.<name>] table per published command.
# <name> must match: [A-Za-z_][A-Za-z0-9_.-]*

# --- Kind 1: wrapper script (default) -------------------------------------
[commands.deploy]
run = "./scripts/deploy.sh"   # arbitrary shell. Invoked as
                              # `sh -c '<run> "$@"'` with project root as CWD.
description = "Deploy stuff"  # shown in `spg help` and zsh completion
args = [
    # Positional (name does not start with '-').
    { name = "target",   description = "environment", values = ["staging", "prod"] },
    { name = "config",   description = "config file", type = "files" },

    # Flag (name starts with '-' or '--').
    { name = "--region", description = "AWS region", values = ["us-east-1", "eu-west-1"] },
    { name = "--dry-run", description = "no changes" },   # boolean (no value/type)
]
complete_hook = "./scripts/deploy.sh __complete"   # optional; see below

# --- Kind 2: shell function (runs in the parent shell) --------------------
[commands.gocd]
description = "cd into a worktree resolved by ./scripts/resolve.sh"
shell_function = 'cd "$(./scripts/resolve.sh "$@")"'
complete_hook = "./scripts/resolve.sh __complete"

# --- Symlinks (optional) --------------------------------------------------
# One [links.<name>] table per symlink this project publishes.
[links.my-skill]
source = "skills/my-skill"     # required; path relative to the repo root,
                               # must exist, must not contain '..'
target = "~/.claude/skills/"   # required; absolute ('~' expanded).
                               # Trailing '/' → link INTO that directory with
                               # <name> as the leaf. No trailing '/' → target
                               # is the exact path of the symlink.
description = "Publish this repo's skill to Claude Code"   # optional
```

Rules:
- Exactly **one** of `run` or `shell_function` is required per command.
  Setting both is an error; setting neither is an error.
- `run` produces a `~/bin/<name>` wrapper. Use it for normal commands.
- `shell_function` produces a function defined in the user's interactive
  shell (sourced at shell start via the completion script). The function
  body is the string verbatim, wrapped in `<name>() { … }`. Use it when the
  command must affect the parent shell: `cd`, `export`, setting shell
  variables, defining aliases — a `~/bin` subprocess can't do these because
  it can't mutate its parent's state. Caveat: shell-function commands are
  only available in **interactive zsh** sessions where the user has
  `source <(spg completion zsh)` in their `~/.zshrc`. They are not on
  `$PATH` and cannot be invoked from cron, scripts, makefiles, etc.
- `values` is a list of fixed strings. Cannot be combined with `type`.
- `type` is `"files"` or `"directories"`. Cannot be combined with `values`.
- Flags with neither `values` nor `type` are treated as boolean (no value
  expected after them).
- Positionals are consumed left-to-right in declared order.
- `args` and `complete_hook` work the same for both kinds.
- `complete_hook` is per-command. It's only consulted when static `args`
  can't answer — i.e., the user is on a positional past the declared list,
  or typing a flag prefix when no flags were declared. Hook output:
  - One candidate per line.
  - `value:description` accepted; bare `value` also accepted.
  - Two special sentinel outputs: `__files__` → file completion; `__directories__` → directory completion.
- Wrapper scripts are deterministic and contain a `# spg-managed:<project>:<command>` marker; do not edit `~/bin/<cmd>` by hand.
- `[links.<name>]` is for repo content a tool expects at a fixed path — a
  Claude Code skill in `~/.claude/skills/`, an editor or CLI config, a
  dotfile. `<name>` may look like a filename (`.zshrc`, `1password`) but
  cannot be `.` or `..`. Missing parent directories of `target` are created.
  Links are created/repaired by `spg install` and `spg sync`, and removed by
  `spg uninstall` — spg only deletes a path it recorded that is still a
  symlink, so it never removes your own files. A foreign symlink or regular
  file sitting at a link's path requires `--force`; a real directory there is
  refused outright (add a trailing `/` to `target` if you meant to link into
  it). Don't declare a link whose `target` lands on a `~/bin/<cmd>` wrapper.
- When you change a `shell_function` body (or switch a command between `run`
  and `shell_function`), the user needs a fresh shell (or to re-source their
  completion script) for the change to take effect. `spg sync` updates the
  registry but cannot reach into already-running shells.

---

## Step 1 — Survey the project

Identify the commands a human would actually want on their `$PATH`. Common
sources, in priority order:

1. **`Makefile` / `justfile` / `Taskfile.yml` targets** — each useful target is
   typically one command (`run = "make <target>"`, `run = "just <recipe>"`).
2. **`package.json` `scripts`** — useful npm scripts (`run = "npm run <name>"`).
3. **`pyproject.toml` `[project.scripts]`** — entry points (`run = "uv run <entry>"`
   if the project uses `uv`, otherwise the appropriate runner).
4. **`scripts/`, `bin/`, `tools/` directories** — shell/Python scripts a human
   invokes directly.
5. **README "Development" / "Usage" sections** — anything documented as
   "run this to …".

Skip:
- One-off setup scripts (`bootstrap.sh`, `install-deps.sh`).
- CI-only entry points.
- Anything whose only sensible invocation is from inside another script.

Prefer **fewer, higher-value commands**. A spg.toml with 30 commands is a
smell; a well-curated spg.toml has 2–8.

---

## Step 2 — Author `spg.toml`

Create `spg.toml` at the repo root.

For each command:
- Pick a short, unambiguous name. Be aware it will land in `~/bin` (for
  `run`-style commands) or be defined as a shell function in interactive zsh
  (for `shell_function`-style commands) — in either case it will shadow
  anything else with that name. Avoid generic names like `build`, `test`,
  `deploy` unless the project clearly owns that namespace. Prefer
  `<project>-<verb>` (e.g. `acme-deploy`) when in doubt.
- Decide on the kind:
  - **`run`** for normal commands (the default).
  - **`shell_function`** only when the command must mutate the parent shell
    — e.g. `cd` into a resolved path, `export` an env var, define an alias.
    If a subprocess could do the job, use `run`.
- Set `run` (or `shell_function`) to the exact shell that works from the
  repo root today.
- Write a `description` that is meaningful in `spg help <cmd>`. One line.
- Fill in `args` for every positional and flag the command accepts. Use
  `values = [...]` for closed sets, `type = "files"` / `"directories"` for
  filesystem args. Skip args you genuinely can't complete usefully.
- Only set `complete_hook` if static `args` are insufficient (see Step 3).

Keep the TOML clean — no trailing commented-out junk, no placeholder commands.

---

## Step 3 — Wire dynamic completion (only when needed)

Set `complete_hook` when candidates are **dynamic** and worth completing:
git branches/tags, hostnames, environments fetched from a config, ticket IDs,
service names from a registry, etc. Skip the hook when:
- The candidate set is small and stable → use `values`.
- The candidate is a path → use `type = "files"` or `"directories"`.
- Listing the candidates is slow (>1–2 seconds) → don't bother; the user will
  type it manually faster than the completion can return.

Implementation pattern:

- Add an `__complete` subcommand (or equivalent) to the script that `run`
  invokes. Convention: positional 1 is the `<position>` (1-indexed against
  args after the command name); positional 2+ are the user's typed words
  (starting with the command name itself).
- Print one candidate per line on stdout. Errors → stderr, non-zero exit code
  → spg drops the result silently.
- Keep the hook fast (sub-second). spg enforces a 2-second timeout.
- Hook CWD is the project root.

Minimal shell hook example:

```sh
# In ./scripts/deploy.sh
case "$1" in
    __complete)
        shift
        position="$1"
        shift
        # words[@] starts with the command name (e.g. "deploy")
        case "$position" in
            1) git branch --format='%(refname:short)' ;;
            2) aws s3 ls | awk '{print $3}' ;;
        esac
        exit 0
        ;;
esac
# … normal command logic …
```

Then in `spg.toml`:

```toml
[commands.deploy]
run = "./scripts/deploy.sh"
complete_hook = "./scripts/deploy.sh __complete"
```

If the project's commands already have a natural completion surface (e.g.,
a Click/Typer app with `--complete`), reuse that — wrap it so the output is
plain `value\n` or `value:description\n` lines.

---

## Step 4 — Install and verify

Run, in order, from the project root:

```sh
spg install      # writes ~/bin/<cmd> wrappers, creates links, registers the project
spg list         # confirm the project, command list, and links
spg help <cmd>   # for each declared command — verify description + args
spg status       # confirm no wrapper/link drift
<cmd> --help     # if the underlying command supports it — sanity check it actually runs
```

For each declared link, confirm the symlink landed where you meant it to
(`ls -l <target>`) and that reading through it reaches the repo content.

If `spg install` errors about a non-spg file already at `~/bin/<cmd>`, the
correct fix is almost always to **rename the spg command** (the user's
existing `~/bin/<cmd>` was there first). Only suggest `--force` if the user
explicitly says they want to overwrite.

Then verify tab completion in a new zsh shell:
- `<cmd> <TAB>` — should offer your positionals/flags.
- `<cmd> --<TAB>` — should list declared flags with descriptions.
- For `complete_hook` commands, exercise the dynamic position you wired.

For `shell_function` commands, also confirm the function itself is defined
in a new interactive zsh:
- `type <cmd>` should report it as a shell function (not "not found").
- Invoking `<cmd>` should produce the parent-shell effect you wanted
  (e.g. the prompt's CWD should change for a `cd`-style command).
- `<cmd>` will not be on `$PATH`; running it from `bash -c`, `sh -c`,
  cron, or a non-interactive zsh will not find it. That's expected.

Completion (and shell-function emission) requires
`source <(spg completion zsh)` in the user's `~/.zshrc`. If it isn't there
yet, mention that — don't edit their dotfiles unless asked.

---

## Output

Make the changes directly:

1. Write `spg.toml` at the repo root.
2. If you added `__complete` subcommands to project scripts, commit those
   edits alongside.
3. Report:
   - The commands you chose to publish and why.
   - Any commands you deliberately skipped.
   - Whether you added any `complete_hook` callbacks, and which positions
     they cover.
   - Anything you couldn't complete cleanly with the current schema (so the
     user knows the rough edges).

Do **not** run `spg install` for the user unless they ask — they may want to
review `spg.toml` first. Tell them the next step is `spg install`.
