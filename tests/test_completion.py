from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from spg import cli, paths
from spg.completion import (
    SENTINEL_DIRS,
    SENTINEL_FILES,
    SPG_SUBCOMMANDS,
    candidates_for_command,
    candidates_for_spg,
    list_managed_commands,
    render_shell_function_defs,
    render_zsh_completion,
)
from spg.registry import Registry


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    bd = tmp_path / "bin"
    bd.mkdir()
    registry = tmp_path / "registry.toml"
    monkeypatch.setattr(paths, "bin_dir", lambda: bd)
    monkeypatch.setattr(paths, "registry_path", lambda: registry)
    monkeypatch.setattr(cli, "bin_dir", lambda: bd)
    monkeypatch.setattr(cli, "registry_path", lambda: registry)
    return {"bin_dir": bd, "registry": registry}


def _registered(env: dict[str, Path]) -> Registry:
    return Registry.load(env["registry"])


def test_candidates_for_spg_first_level(isolated_env: dict[str, Path]) -> None:
    cands = candidates_for_spg(["spg", ""], current=2, registry=_registered(isolated_env))
    names = [c.split(":", 1)[0] for c in cands]
    assert "install" in names
    assert "completion" in names


def test_spg_subcommands_match_the_cli() -> None:
    """SPG_SUBCOMMANDS is a hand-maintained mirror of the CLI — keep it honest.

    `completion` sits below `cli` in the dependency chain and can't import it, so
    this test is what stops the completion table's names and descriptions from
    drifting away from the commands the CLI actually exposes.
    """
    visible = {name: cmd for name, cmd in cli.cli.commands.items() if not cmd.hidden}
    assert {name for name, _ in SPG_SUBCOMMANDS} == set(visible)
    for name, description in SPG_SUBCOMMANDS:
        expected = visible[name].get_short_help_str(limit=200).rstrip(".")
        assert description == expected, f"{name}: completion says {description!r}, CLI {expected!r}"


def test_candidates_for_spg_help_lists_managed_commands(
    isolated_env: dict[str, Path], make_project
) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    cands = candidates_for_spg(["spg", "help", ""], current=3, registry=_registered(isolated_env))
    assert "hello" in cands


def test_candidates_for_spg_uninstall_lists_projects(
    isolated_env: dict[str, Path], make_project
) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    cands = candidates_for_spg(
        ["spg", "uninstall", ""], current=3, registry=_registered(isolated_env)
    )
    assert "demo" in cands


def test_candidates_for_spg_dir_flag(isolated_env: dict[str, Path]) -> None:
    cands = candidates_for_spg(
        ["spg", "install", "-C", ""], current=4, registry=_registered(isolated_env)
    )
    assert cands == [SENTINEL_DIRS]


def test_candidates_for_command_positional_values(
    isolated_env: dict[str, Path], tmp_path: Path
) -> None:
    project = tmp_path / "deploy_proj"
    project.mkdir()
    (project / "spg.toml").write_text(
        dedent(
            """\
            [project]
            name = "deploy_proj"

            [commands.deploy]
            run = "./scripts/deploy.sh"
            args = [
                { name = "target", values = ["staging", "prod"] },
                { name = "config", type = "files" },
                { name = "--region", values = ["us-east-1"] },
                { name = "--dry-run" },
            ]
            """
        )
    )
    (project / "scripts").mkdir()
    (project / "scripts" / "deploy.sh").write_text("#!/bin/sh\n")
    (project / "scripts" / "deploy.sh").chmod(0o755)
    cli.main(["install", "-C", str(project)])

    reg = _registered(isolated_env)

    # First positional → "target" values
    cands = candidates_for_command("deploy", ["deploy", ""], current=2, registry=reg)
    assert any(c.startswith("staging") for c in cands)
    assert any(c.startswith("prod") for c in cands)

    # Second positional → files sentinel
    cands = candidates_for_command("deploy", ["deploy", "staging", ""], current=3, registry=reg)
    assert cands == [SENTINEL_FILES]

    # `--region <TAB>` → values
    cands = candidates_for_command("deploy", ["deploy", "--region", ""], current=3, registry=reg)
    assert any(c.startswith("us-east-1") for c in cands)

    # Current word starts with `-` → flag names
    cands = candidates_for_command("deploy", ["deploy", "-"], current=2, registry=reg)
    names = [c.split(":", 1)[0] for c in cands]
    assert "--region" in names
    assert "--dry-run" in names

    # Boolean flag does not steal the next positional slot
    cands = candidates_for_command("deploy", ["deploy", "--dry-run", ""], current=3, registry=reg)
    assert any(c.startswith("staging") for c in cands)


def test_candidates_for_command_hook(isolated_env: dict[str, Path], tmp_path: Path) -> None:
    project = tmp_path / "hooked"
    project.mkdir()
    hook = project / "scripts" / "hook.sh"
    hook.parent.mkdir()
    hook.write_text(
        "#!/bin/sh\n"
        "# protocol: $1=position, $2.. = words after command name\n"
        "echo alpha\n"
        "echo beta:beta description\n"
    )
    hook.chmod(0o755)
    (project / "spg.toml").write_text(
        dedent(
            """\
            [project]
            name = "hooked"

            [commands.x]
            run = "./scripts/hook.sh"
            complete_hook = "./scripts/hook.sh __complete"
            """
        )
    )
    cli.main(["install", "-C", str(project)])
    reg = _registered(isolated_env)
    cands = candidates_for_command("x", ["x", ""], current=2, registry=reg)
    assert "alpha" in cands
    assert "beta:beta description" in cands


def test_candidates_for_command_hook_supplies_dash_prefix(
    isolated_env: dict[str, Path], tmp_path: Path
) -> None:
    project = tmp_path / "dyn"
    project.mkdir()
    hook = project / "scripts" / "hook.sh"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\necho --alpha:dynamic alpha flag\necho --beta:dynamic beta flag\n")
    hook.chmod(0o755)
    (project / "spg.toml").write_text(
        dedent(
            """\
            [project]
            name = "dyn"

            [commands.dyn]
            run = "./scripts/hook.sh"
            complete_hook = "./scripts/hook.sh __complete"
            """
        )
    )
    cli.main(["install", "-C", str(project)])
    reg = _registered(isolated_env)
    cands = candidates_for_command("dyn", ["dyn", "--"], current=2, registry=reg)
    assert "--alpha:dynamic alpha flag" in cands
    assert "--beta:dynamic beta flag" in cands


def test_candidates_for_command_spg_delegates(isolated_env: dict[str, Path], make_project) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    reg = _registered(isolated_env)
    # cmd-namespace dispatch for the name "spg" should behave like spg-namespace
    cands = candidates_for_command("spg", ["spg", ""], current=2, registry=reg)
    names = [c.split(":", 1)[0] for c in cands]
    assert "install" in names


def test_list_managed_commands_orders_and_dedupes(
    isolated_env: dict[str, Path], tmp_path: Path
) -> None:
    p1 = tmp_path / "p1"
    p1.mkdir()
    (p1 / "spg.toml").write_text(
        '[project]\nname = "p1"\n[commands.zeta]\nrun = "./x"\n[commands.alpha]\nrun = "./x"\n'
    )
    p2 = tmp_path / "p2"
    p2.mkdir()
    (p2 / "spg.toml").write_text('[project]\nname = "p2"\n[commands.mid]\nrun = "./x"\n')
    cli.main(["install", "-C", str(p1), "--force"])
    cli.main(["install", "-C", str(p2), "--force"])
    reg = _registered(isolated_env)
    assert list_managed_commands(reg) == ["alpha", "mid", "zeta"]


def test_render_zsh_completion_shape() -> None:
    script = render_zsh_completion()
    assert "#compdef spg" in script
    assert "_spg_dispatch" in script
    assert "_spg_cmd_dispatch" in script
    assert "_spg_handle spg" in script
    assert "spg __complete" in script
    assert "list-commands" in script
    assert "list-shell-functions" in script
    assert "__files__" in script
    assert "__directories__" in script


def test_render_shell_function_defs(isolated_env: dict[str, Path], tmp_path: Path) -> None:
    project = tmp_path / "fn"
    project.mkdir()
    (project / "spg.toml").write_text(
        dedent(
            """\
            [project]
            name = "fn"

            [commands.plain]
            run = "./scripts/x"

            [commands.gocd]
            shell_function = 'cd "$(./scripts/resolve.sh "$@")"'

            [commands.multi]
            shell_function = \"\"\"
            local target
            target="$(./scripts/pick.sh "$@")" || return 1
            cd "$target"
            \"\"\"
            """
        )
    )
    (project / "scripts").mkdir()
    (project / "scripts" / "x").write_text("#!/bin/sh\n")
    (project / "scripts" / "x").chmod(0o755)
    cli.main(["install", "-C", str(project)])
    reg = _registered(isolated_env)
    defs = render_shell_function_defs(reg)
    assert 'gocd() {\ncd "$(./scripts/resolve.sh "$@")"\n}' in defs
    assert "multi() {" in defs
    assert 'cd "$target"' in defs
    # `plain` is a wrapper command, no function emitted for it.
    assert "plain() {" not in defs


def test_internal_complete_list_shell_functions(
    isolated_env: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "fn"
    project.mkdir()
    (project / "spg.toml").write_text(
        dedent(
            """\
            [project]
            name = "fn"

            [commands.gocd]
            shell_function = 'cd "$(echo "$@")"'
            """
        )
    )
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()
    rc = cli.main(["__complete", "list-shell-functions"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "gocd() {" in out
    assert 'cd "$(echo "$@")"' in out
    assert out.rstrip().endswith("}")


def test_shell_function_still_listed_in_list_commands(
    isolated_env: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "fn"
    project.mkdir()
    (project / "spg.toml").write_text(
        dedent(
            """\
            [project]
            name = "fn"

            [commands.gocd]
            shell_function = 'cd .'
            """
        )
    )
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()
    rc = cli.main(["__complete", "list-commands"])
    assert rc == 0
    # compdef bindings for shell-function commands rely on list-commands output.
    assert "gocd" in capsys.readouterr().out


def test_internal_complete_subcommand(
    isolated_env: dict[str, Path], make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()

    rc = cli.main(["__complete", "list-commands"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hello" in out

    rc = cli.main(["__complete", "list-projects"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo" in out

    rc = cli.main(["__complete", "spg", "2", "spg", ""])
    assert rc == 0
    out = capsys.readouterr().out
    assert "install" in out

    rc = cli.main(["__complete", "cmd", "hello", "2", "hello", ""])
    assert rc == 0  # no completions configured for hello's positional, but should not crash


def test_completion_zsh_subcommand_prints_script(
    isolated_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["completion", "zsh"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "#compdef spg" in out


def test_completion_no_shell_argument_errors(
    isolated_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["completion"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "supported shells" in err
