# CLAUDE.md

This project's agent guidance lives in [AGENTS.md](AGENTS.md) — read it first.

It covers the architecture map, the module dependency chain, the
`uv run pytest` / `uv run ty check` gates, and the safety conventions
(atomic writes, registry locking, never clobbering non-spg files) that matter
when changing this codebase.

Quick reference:

- Run things with `uv` (`uv run spg`, `uv run pytest`, `uv run ty check`).
- All `spg.toml` parsing/validation belongs in `src/spg/config.py`.
- Both `pytest` and `ty check` must pass before a change is done.
- Don't push, merge, or touch CI unless asked; open PRs with `gw pr open`.
