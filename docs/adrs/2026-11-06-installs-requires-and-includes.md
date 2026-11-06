---
id: 2026-11-06-installs-requires-and-includes
status: Proposed
supersedes: null
superseded-by: null
components: [config, installer, provision, registry, cli, completion]
ticket: null
date: 2026-11-06
---
# Let spg.toml declare installs, order items with `requires`, and split across included files

## Context

spg publishes commands and symlinks. People who use it for dotfiles want the
same file to bootstrap a machine: install Homebrew, a handful of brew packages,
uv, oh-my-zsh. Today that lives in a separate `bootstrap.sh`, which spg cannot
see. That causes an ordering bug: oh-my-zsh's installer writes its own
`~/.zshrc`, so if spg links `.zshrc` first, the installer moves the link aside
and replaces it. The `.zshrc` link has to wait until oh-my-zsh is present, and
should not be created at all if oh-my-zsh failed to install.

Two properties of spg shape the answer:

- **`spg install` and `spg sync` reconcile.** They re-read `spg.toml` and bring
  the machine in line with it, every time. Anything new has to be safe to run
  over and over.
- **spg never removes what it did not create, and never deletes user data.**
  It can remove a wrapper or a symlink because both are cheap to recreate and
  spg can tell they are its own. It cannot safely uninstall software.

A dotfiles config that covers installs, commands and links for zsh, git, an
editor and a dozen tools outgrows one file quickly.

## Decision

**A new `[installs.<name>]` table.** An install is a shell `run` plus an
idempotency probe: a shell `check` (exit 0 means already installed) or a
`creates` path (exists means already installed). Exactly one of the two is
required, so spg never reruns an installer just because it cannot tell
whether the software is already there. `brew = [...]` and `cask = [...]`
shorthands generate the `run` and `check` for Homebrew packages, and look for
`brew` at `/opt/homebrew/bin` and `/usr/local/bin` too, because right after a
fresh Homebrew install it is not yet on `PATH`. After a `run`, spg runs the
probe again. If it still fails, the install counts as failed. That catches the
common case of a tool installed somewhere that is not on `PATH`.

Installs run on `spg install`, `spg sync` and `spg enable`, because the probe
makes rerunning cheap and a sync should pick up a newly added package.
`--skip-installs` turns them off for one run. spg never uninstalls anything,
and `spg uninstall` leaves installed software alone.

**A `requires = [...]` field on installs, commands and links.** It takes the
same selectors users already type for `spg disable` (`NAME`, `install:NAME`,
`cmd:NAME`, `link:NAME`), resolved and cycle-checked when `spg.toml` loads.
Items run in dependency order. Otherwise installs go first, then commands,
then links, each in declaration order, so a bootstrap install comes before
the dotfiles that depend on it even without `requires`. If something fails,
every item that depends on it, directly or through other items, is skipped
and reported. A skipped item keeps its previous state: an existing wrapper or
link is neither refreshed nor removed, and its registry record stays.
`requires` only refers to items in the same project.

**Users can decline installs** with `spg install --without install:NAME`,
`spg disable` and the checklist, the same way as commands and links. The
choice is stored as `excluded_installs` on the registry entry, following the
exclusions ADR (2026-08-11). **A declined dependency counts as satisfied.**
Declining an install means "I take care of this myself", so it must not block
the items that depend on it.

**`[project].include = [...]`** lists more TOML files, as paths or globs
relative to the project root. They can declare `[commands]`, `[links]` and
`[installs]`, but not `[project]` or another `include`. Matches are loaded in
sorted order. A literal path that does not exist is an error. A glob that
matches nothing is not, so an empty `spg.d/` is fine. If two files declare the
same item, that is an error naming both files. Link `source` paths stay
relative to the project root, not to the included file, so moving a
declaration between files changes nothing.

## Consequences

- `install`, `sync` and `enable` now run code from the project's `spg.toml`.
  Wrapper commands already ran project code, but only when the user invoked
  them. A `sync` across every registered project can now run installers. That
  is a real change in trust, and the price of the feature. `--skip-installs`
  turns it off.
- Installs run inside `Registry.locked()`. A slow `brew install` holds the
  lock, so a second spg command waits. That is the intended serialization,
  but `spg sync` can now take minutes where it used to take milliseconds.
- `spg install`, `spg sync` and `spg enable` can now partly succeed. The
  installer no longer either finishes everything or raises `InstallError`
  before writing anything. Failed installs and the items skipped because of
  them are reported, the registry is still saved for everything that did
  succeed, and the command exits 1.
- `spg status` runs every install's probe (with a timeout) and reports missing
  installs as problems. That means status runs project shell code too.
- The registry gains `excluded_installs` only. Like the other exclusion keys
  it is additive, so `REGISTRY_VERSION` stays at 1. The registry does not
  record installs as installed, because the probe is the source of truth.
- Every reader of `spg.toml` goes through `load_project_config`, so includes
  work everywhere without other code knowing about them.

## Alternatives considered

- **A separate `spg bootstrap` command.** It keeps install/sync free of side
  effects, but brings back the ordering problem: the `.zshrc` link is created
  by `install`, and nothing connects it to the oh-my-zsh install that has to
  run first.
- **Ordering installs before everything else, with no `requires`.** It covers
  oh-my-zsh before `.zshrc`, but it cannot skip the link when the install
  fails, and it cannot express one install needing another, such as brew
  packages needing Homebrew.
- **Making `check` optional and always running `run`.** Fewer fields, but a
  `curl | sh` installer run on every sync would be a disaster.
- **Built-in package-manager plugins** (apt, mas, npm, …). Only Homebrew was
  asked for. Anything else fits in `run` + `check` until it comes up often
  enough to deserve a shorthand.
- **Picking up `spg.d/*.toml` automatically with no `include` key.** Less to
  write, but a file appearing in the repo would quietly change what gets
  installed. Listing includes in `[project]` keeps the root file the single
  place that says what is loaded.
