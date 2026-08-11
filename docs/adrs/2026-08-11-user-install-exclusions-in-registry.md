---
id: 2026-08-11-user-install-exclusions-in-registry
status: Accepted
supersedes: null
superseded-by: null
components: [registry, installer, cli, completion]
ticket: shr3kst3r/spg#6
date: 2026-08-11
---
# Record a user's declined commands and links in the spg registry, not in spg.toml

## Context

`spg.toml` is the project's declaration of what it publishes, and it is
version-controlled and shared by everyone who clones the repo. Until now that
declaration has been the *only* input to installation: `spg install` and
`spg sync` both call `installer._install_project_locked`, which iterates
`config.commands` and `config.links` unconditionally. There is no representation
anywhere of "this machine's user does not want that one."

Issue #6 asks for exactly that representation: everything installs by default,
and the end user can decline individual commands or links.

The constraint that decides this is `spg sync`. It re-reads every registered
project's `spg.toml` from disk and funnels into the same install path, and that
path *reconciles in both directions* — it removes wrappers a project no longer
declares, and recreates anything declared that is missing. So a decline that
lives only in the invocation (a bare `--without` flag with nothing stored) is
undone by the next `sync`, and so is a hand-deleted `~/bin/<cmd>`. Whatever
holds the user's choice has to be persistent, per-machine, and consulted on
every reconciliation.

Two candidate homes exist. The registry at `~/.config/spg/registry.toml` is
already per-user, per-machine, keyed by project, written atomically under an
exclusive `flock`, and refreshed from disk inside that lock — and it is already
the authority for "what did we install for this project", which is what every
other read site (`spg help`, completion, `prune_orphan_wrappers`, `spg status`'s
wrapper cross-check) derives from. The alternative is a second file holding user
intent separately from installed state.

There is a wrinkle either way: the registry's own header calls it a "managed
file, edit with care", and `_format_registry` rewrites it wholesale from parsed
dataclasses on every save, so anything stored there must be explicitly
round-tripped or it is silently dropped.

## Decision

We record each user's declined items as additive per-project keys on the
registry entry — `excluded_commands` and `excluded_links`, lists of declaration
names — and we resolve them inside the registry lock, before the installer
touches the filesystem.

The registry entry's existing `commands` and `links` keys keep their current
meaning: **what is actually installed**, with declined items absent. Exclusions
are recorded separately as the reason for that absence. Every consumer that
already derives from the registry therefore needs no change to honor a decline.

`spg.toml` gains no new fields. A project cannot express "this command is
optional" or pre-decline anything on a user's behalf; declining is exclusively a
user-side act.

## Consequences

Reconciliation stops being a pure function of `spg.toml` and becomes a function
of `spg.toml` *and* per-machine user state. That is the real cost of this
decision, and it lands in a few places:

- **Exclusions must be resolved inside `Registry.locked()`.** The lock refreshes
  in-memory state from disk, discarding anything a caller read beforehand, so a
  pre-lock read of stored exclusions is unsafe to act on. `cli.py` may read them
  to pre-fill a prompt; only the installer may decide with them.
- **Any code path that reads `spg.toml` directly, bypassing the registry, now
  leaks declined items.** Two such paths exist today and are wrong until fixed:
  `completion.render_shell_function_defs` (a declined `shell_function` command
  would still be sourced into the user's shell) and `cli._link_problems` (every
  declined link becomes a phantom `spg status` problem, holding exit 1 forever).
  This is a standing constraint on future work: prefer deriving from the
  registry entry, and where reading the config is unavoidable, filter it.
- **`REGISTRY_VERSION` stays at 1.** These are purely additive keys, which
  `registry.py`'s own rule says do not bump the version. The accepted cost is
  that an *older* `spg` reading a newer registry ignores the exclusions and
  silently re-installs the declined items on its next `sync`. We prefer that to
  a version bump, which would make an older `spg` refuse the registry outright
  and break every command on that machine until the binary is upgraded. The
  blast radius of the silent case is one user mid-upgrade; the blast radius of
  the loud case is that user's entire toolchain.
- **`spg uninstall` forgets a project's exclusions,** because it drops the whole
  registry entry. Reinstalling gets everything again. This is consistent with
  "everything by default" but it means uninstall/reinstall is not a safe way to
  repair an install.
- **A stored exclusion for a name the project no longer declares is kept, not
  pruned.** Honoring intent across an upstream `spg.toml` that drops and later
  restores a command matters more than tidiness, and erroring on stored names
  would let one upstream edit break `spg sync` machine-wide. Stale exclusions
  are surfaced informationally, never as a failure.
- Declined items are excluded from conflict detection, so a declined command can
  no longer fail an install over a `~/bin` collision it does not cause. That is
  intended, and it is also load-bearing: it is what makes declining a usable
  escape hatch when a project's command name collides with something you own.

What gets easier: the user-facing surfaces are thin. Because the decision is
"filter the `ProjectConfig` under the lock", `--without` at install time,
`spg disable`/`enable` afterwards, and an interactive checklist are three ways
to produce the same small change to one stored set, and removal of a
newly-declined item falls out of the installer's existing orphan-cleanup logic
rather than needing its own path.

## Alternatives considered

- **A separate `~/.config/spg/overrides.toml`** — a cleaner conceptual split
  between user intent and derived installed-state, and it would survive
  `spg uninstall`. Rejected because it doubles the machinery that has to be
  right: a second parse path, a second atomic-write and locking story, a second
  version field, and a two-file consistency question at every install — all for
  state that is meaningless without a registry entry to attach it to.
- **New optional/default fields in `spg.toml`** (e.g. `optional = true`, or a
  project-declared default-off) — wrong actor. `spg.toml` is shared and
  version-controlled, so one user's choice would land in everyone's clone. This
  does not preclude a project later declaring an item optional; it says that is
  a different decision from the user declining one.
- **A gitignored per-repo `spg.local.toml`** — right actor, wrong scope. It
  fragments across git worktrees (which this project itself uses, so the same
  logical project would hold several independent opt-out sets), and it puts
  machine state inside a repo the user may delete and re-clone.
- **`--without` as a pure invocation flag, nothing stored** — the smallest
  change, and genuinely appealing until `spg sync` undoes it. Ruled out by the
  reconciliation behavior described above, not by preference.
- **Teaching the installer to leave any pre-existing absence alone** (treat a
  missing wrapper as an implicit decline) — no stored state at all, but it makes
  "declined" indistinguishable from "install was interrupted" or "user deleted
  it by accident", and it would silently disable `spg status`'s ability to
  report a genuinely missing wrapper, which is most of what that command is for.
