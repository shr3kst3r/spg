from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from spg import cli, paths


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


def test_init_creates_starter_config(
    isolated_env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "newproj"
    target.mkdir()
    rc = cli.main(["init", "-C", str(target), "--name", "newproj"])
    assert rc == 0
    config_file = target / "spg.toml"
    assert config_file.exists()
    text = config_file.read_text()
    assert 'name = "newproj"' in text
    out = capsys.readouterr().out
    assert "Wrote" in out


def test_init_refuses_overwrite(
    isolated_env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "newproj"
    target.mkdir()
    (target / "spg.toml").write_text('[project]\nname = "x"\n')
    rc = cli.main(["init", "-C", str(target)])
    assert rc == 1


def test_install_list_uninstall_flow(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    rc = cli.main(["install", "-C", str(project)])
    assert rc == 0
    assert (isolated_env["bin_dir"] / "hello").exists()

    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo" in out
    assert "hello" in out
    # rich summary footer reflects the project/command counts
    assert "1 project" in out

    rc = cli.main(["uninstall", "-C", str(project)])
    assert rc == 0
    assert not (isolated_env["bin_dir"] / "hello").exists()


def test_list_empty(isolated_env, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No projects registered" in out


def test_short_path_collapses_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    assert cli._short_path(tmp_path) == "~"
    assert cli._short_path(tmp_path / "bin") == "~/bin"
    assert cli._short_path("/elsewhere/bin") == "/elsewhere/bin"


def test_help_for_registered_command(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()  # drain

    rc = cli.main(["help", "hello"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hello" in out
    assert "Say hello" in out
    assert "who" in out


def test_help_unknown_command(isolated_env, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["help", "nope"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no registered command" in err
    # Dead end → hint: point at the listing instead.
    assert "spg help" in err


def test_help_unknown_command_suggests_close_match(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()

    rc = cli.main(["help", "helo"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "did you mean" in err
    assert "hello" in err


def test_help_without_name_lists_commands(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    other = make_project(
        "tools",
        dedent("""\
            [commands.gocd]
            description = "cd into the project"
            shell_function = 'cd "$(pwd)"'
            """),
    )
    cli.main(["install", "-C", str(project)])
    cli.main(["install", "-C", str(other)])
    capsys.readouterr()

    rc = cli.main(["help"])
    assert rc == 0
    out = capsys.readouterr().out
    # Every registered command, grouped under its owning project.
    assert "demo" in out
    assert "hello" in out
    assert "Say hello" in out
    assert "tools" in out
    assert "gocd" in out
    assert "cd into the project" in out
    assert "shell function" in out
    assert "spg help <command>" in out


def test_help_without_name_on_empty_registry(
    isolated_env, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["help"])
    assert rc == 0
    assert "No projects registered" in capsys.readouterr().out


def test_help_without_name_reports_command_missing_from_config(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    """The listing reports registry/spg.toml drift instead of failing on it."""
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    (project / "spg.toml").unlink()
    capsys.readouterr()

    rc = cli.main(["help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hello" in out
    assert "unreadable" in out


def test_status_clean(isolated_env, make_project, capsys: pytest.CaptureFixture[str]) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()

    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "All good" in out


def test_status_flags_orphan(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()

    # Orphan: managed wrapper for an unregistered project
    (isolated_env["bin_dir"] / "ghost").write_text(
        "#!/bin/sh\n# spg-managed: phantom:ghost\nexec true\n"
    )

    rc = cli.main(["status"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "orphan wrappers" in out


def test_sync_warns_when_config_missing(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()

    (project / "spg.toml").unlink()
    rc = cli.main(["sync"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "is missing" in err


def test_sync_prunes_orphan_wrappers(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    (isolated_env["bin_dir"] / "ghost").write_text(
        "#!/bin/sh\n# spg-managed: phantom:ghost\nexec true\n"
    )
    capsys.readouterr()

    rc = cli.main(["sync"])
    assert rc == 0
    assert not (isolated_env["bin_dir"] / "ghost").exists()
    assert (isolated_env["bin_dir"] / "hello").exists()
    out = capsys.readouterr().out
    assert "Pruned" in out
    assert "ghost" in out


def test_sync_keeps_wrappers_of_project_that_failed_to_load(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()

    # demo's config disappears, and a stale wrapper claims demo ownership;
    # sync must warn about demo without pruning anything it claims to own.
    (isolated_env["bin_dir"] / "stale").write_text(
        "#!/bin/sh\n# spg-managed: demo:stale\nexec true\n"
    )
    (project / "spg.toml").unlink()

    rc = cli.main(["sync"])
    assert rc == 1
    assert (isolated_env["bin_dir"] / "stale").exists()
    assert (isolated_env["bin_dir"] / "hello").exists()


# --- [links] ---------------------------------------------------------------


def link_config(project: Path, home: Path, target: str = "{home}/.claude/skills/") -> None:
    skill = project / "skills" / "my-skill"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("# skill\n")
    (project / "spg.toml").write_text(
        dedent(f"""\
            [project]
            name = "demo"

            [commands.hello]
            run = "./scripts/hello.sh"

            [links.my-skill]
            source = "skills/my-skill"
            target = "{target.format(home=home)}"
            description = "Publish the skill"
        """)
    )


def test_install_status_uninstall_with_links(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    home = tmp_path / "home"
    link_config(project, home)

    assert cli.main(["install", "-C", str(project)]) == 0
    out = capsys.readouterr().out
    assert "linked" in out and "my-skill" in out
    link = home / ".claude" / "skills" / "my-skill"
    assert link.is_symlink()

    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "Links" in out
    assert "my-skill" in out
    assert "1 link" in out

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Managed links" in out
    assert "All good" in out

    assert cli.main(["uninstall", "demo"]) == 0
    out = capsys.readouterr().out
    assert "unlinked" in out
    assert not link.exists()


def test_status_reports_missing_and_repointed_links(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    home = tmp_path / "home"
    link_config(project, home)
    assert cli.main(["install", "-C", str(project)]) == 0
    capsys.readouterr()
    link = home / ".claude" / "skills" / "my-skill"

    # Repointed by hand.
    link.unlink()
    link.symlink_to(tmp_path / "elsewhere")
    assert cli.main(["status"]) == 1
    assert "points at" in capsys.readouterr().out

    # Gone entirely.
    link.unlink()
    assert cli.main(["status"]) == 1
    assert "missing link" in capsys.readouterr().out

    # sync puts it back.
    assert cli.main(["sync"]) == 0
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    assert "All good" in capsys.readouterr().out


def test_status_reports_stale_registered_link(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    home = tmp_path / "home"
    link_config(project, home)
    assert cli.main(["install", "-C", str(project)]) == 0
    capsys.readouterr()

    # Drop the link declaration without syncing.
    (project / "spg.toml").write_text(
        dedent("""\
            [project]
            name = "demo"

            [commands.hello]
            run = "./scripts/hello.sh"
        """)
    )
    assert cli.main(["status"]) == 1
    assert "stale link" in capsys.readouterr().out

    assert cli.main(["sync"]) == 0
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    assert not (home / ".claude" / "skills" / "my-skill").exists()


def test_install_error_message_names_the_link_table(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error text keeps its TOML table names instead of losing them to rich markup."""
    project = make_project("demo")
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    link_config(project, home, target="{home}/skills")

    assert cli.main(["install", "-C", str(project)]) == 1
    err = capsys.readouterr().err
    assert "[links.my-skill].target" in err


def test_config_error_keeps_command_table_name(
    isolated_env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "broken"
    project.mkdir()
    (project / "spg.toml").write_text('[project]\nname = "broken"\n\n[commands.oops]\nrun = 1\n')
    assert cli.main(["install", "-C", str(project)]) == 1
    assert "[commands.oops].run must be a string" in capsys.readouterr().err
