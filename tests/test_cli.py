from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from spg import cli, paths
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


# --- declining commands and links -------------------------------------------


def excludable_config(project: Path, home: Path) -> None:
    """spg.toml with two commands and one link, for decline tests."""
    skill = project / "skills" / "my-skill"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("# skill\n")
    (project / "spg.toml").write_text(
        dedent(f"""\
            [project]
            name = "demo"

            [commands.alpha]
            run = "./scripts/hello.sh"
            description = "Alpha"

            [commands.beta]
            run = "./scripts/hello.sh"
            description = "Beta"

            [links.my-skill]
            source = "skills/my-skill"
            target = "{home}/.claude/skills/"
            description = "Publish the skill"
        """)
    )


def test_install_without_selector(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")

    rc = cli.main(
        ["install", "-C", str(project), "--without", "beta", "--without", "link:my-skill"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "disabled" in out
    assert "cmd:beta" in out
    assert "link:my-skill" in out
    assert (isolated_env["bin_dir"] / "alpha").exists()
    assert not (isolated_env["bin_dir"] / "beta").exists()
    assert not (tmp_path / "home" / ".claude" / "skills" / "my-skill").exists()


def test_install_without_unknown_selector_errors(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")

    rc = cli.main(["install", "-C", str(project), "--without", "beeta"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "declares no [commands.beeta] or [links.beeta]" in err
    assert "cmd:beta" in err


def test_install_without_and_interactive_is_a_usage_error(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    rc = cli.main(["install", "-C", str(project), "--without", "hello", "-i"])
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_interactive_requires_a_terminal(
    isolated_env, make_project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    rc = cli.main(["install", "-C", str(project), "-i"])
    assert rc == 2
    assert "requires a terminal" in capsys.readouterr().err


def test_disable_and_enable_roundtrip(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    assert cli.main(["install", "-C", str(project)]) == 0
    link = tmp_path / "home" / ".claude" / "skills" / "my-skill"
    assert (isolated_env["bin_dir"] / "beta").exists()
    assert link.is_symlink()
    capsys.readouterr()

    assert cli.main(["disable", "-C", str(project), "beta", "link:my-skill"]) == 0
    out = capsys.readouterr().out
    assert "Updated" in out
    assert "cmd:beta" in out
    assert not (isolated_env["bin_dir"] / "beta").exists()
    assert not link.exists()

    assert cli.main(["enable", "-C", str(project), "beta", "link:my-skill"]) == 0
    out = capsys.readouterr().out
    assert (isolated_env["bin_dir"] / "beta").exists()
    assert link.is_symlink()
    assert "disabled" not in out


def test_a_decline_is_reported_instead_of_no_changes(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    """With everything declined there is nothing to write — but that is not "no changes."."""
    project = make_project("solo", '[commands.solo]\nrun = "./scripts/hello.sh"\n')
    assert cli.main(["install", "-C", str(project), "--without", "solo"]) == 0
    out = capsys.readouterr().out
    assert "no changes" not in out
    assert "cmd:solo" in out

    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "no changes" not in out
    assert "cmd:solo" in out


def test_disable_requires_at_least_one_selector(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()
    assert cli.main(["disable", "-C", str(project)]) == 2


def test_disable_on_unregistered_project_errors(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    rc = cli.main(["disable", "-C", str(project), "hello"])
    assert rc == 1
    assert "run `spg install` first" in capsys.readouterr().err


def test_status_is_clean_with_a_declined_link(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A declined link is expected to be absent, so it is not status drift.

    Without this, every declined link would be a phantom problem holding
    `spg status` at exit 1 forever.
    """
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    assert cli.main(["install", "-C", str(project), "--without", "link:my-skill"]) == 0
    capsys.readouterr()

    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "All good" in out


def test_status_is_clean_with_a_declined_command(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    assert cli.main(["install", "-C", str(project), "--without", "beta"]) == 0
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    assert "All good" in capsys.readouterr().out


def test_status_notes_a_stale_exclusion_without_failing(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    assert cli.main(["install", "-C", str(project), "--without", "beta"]) == 0

    # Upstream drops the declined command; the exclusion is kept, not pruned.
    (project / "spg.toml").write_text(
        dedent("""\
            [project]
            name = "demo"

            [commands.alpha]
            run = "./scripts/hello.sh"
        """)
    )
    assert cli.main(["sync"]) == 0
    capsys.readouterr()

    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "no longer declared" in out
    assert "cmd:beta" in out


def test_list_shows_declined_items(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    cli.main(["install", "-C", str(project), "--without", "beta"])
    capsys.readouterr()

    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "Disabled" in out
    assert "cmd:beta" in out


def test_list_omits_the_disabled_column_when_nothing_is_declined(
    isolated_env, make_project, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()
    assert cli.main(["list"]) == 0
    assert "Disabled" not in capsys.readouterr().out


def test_help_does_not_list_a_declined_command(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    cli.main(["install", "-C", str(project), "--without", "beta"])
    capsys.readouterr()

    assert cli.main(["help"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" not in out


def test_interactive_answer_is_authoritative(
    isolated_env, make_project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checklist replaces the stored set rather than adding to it."""
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    config = cli.load_project_config_from_dir(project)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.click, "prompt", lambda *a, **k: "2")

    change = cli._interactive_select(
        config,
        excluded_commands=("alpha",),
        excluded_links=("my-skill",),
    )
    # Items are numbered commands-then-links: 1=alpha, 2=beta, 3=my-skill.
    assert change.disable_commands == ("beta",)
    assert change.disable_links == ()
    assert set(change.enable_commands) == {"alpha"}
    assert set(change.enable_links) == {"my-skill"}


def test_interactive_reprompts_on_bad_input(
    isolated_env,
    make_project,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    config = cli.load_project_config_from_dir(project)
    answers = iter(["nope", "9", "3"])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.click, "prompt", lambda *a, **k: next(answers))

    change = cli._interactive_select(config, excluded_commands=(), excluded_links=())
    assert change.disable_links == ("my-skill",)
    err = capsys.readouterr().err
    assert "is not a number" in err
    assert "out of range" in err


def test_interactive_install_end_to_end(
    isolated_env,
    make_project,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.click, "prompt", lambda *a, **k: "2")

    assert cli.main(["install", "-C", str(project), "-i"]) == 0
    out = capsys.readouterr().out
    assert "Alpha" in out  # the checklist rendered the declarations
    assert "cmd:beta" in out
    assert (isolated_env["bin_dir"] / "alpha").exists()
    assert not (isolated_env["bin_dir"] / "beta").exists()


def test_interactive_keeps_a_stale_exclusion(
    isolated_env, make_project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checklist is authoritative only over what it showed.

    A decline for a name spg.toml no longer declares is not on the list, so the
    user cannot have answered about it — per the ADR it is kept, not pruned.
    """
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    config = cli.load_project_config_from_dir(project)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.click, "prompt", lambda *a, **k: "")

    change = cli._interactive_select(
        config,
        excluded_commands=("alpha", "gone"),
        excluded_links=("gone-link",),
    )
    assert change.enable_commands == ("alpha",)
    assert change.enable_links == ()


def test_enable_clears_a_stale_exclusion(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`spg enable` is the only way to forget a decline the project dropped.

    `spg status` names the stale selector, so `enable` has to accept it even
    though the current spg.toml no longer declares it.
    """
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    assert cli.main(["install", "-C", str(project), "--without", "beta"]) == 0
    (project / "spg.toml").write_text(
        dedent("""\
            [project]
            name = "demo"

            [commands.alpha]
            run = "./scripts/hello.sh"
        """)
    )
    assert cli.main(["sync"]) == 0
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    assert "run `spg enable cmd:beta`" in capsys.readouterr().out

    assert cli.main(["enable", "-C", str(project), "cmd:beta"]) == 0
    capsys.readouterr()
    entry = Registry.load(isolated_env["registry"]).projects["demo"]
    assert entry.excluded_commands == ()

    assert cli.main(["status"]) == 0
    assert "no longer declared" not in capsys.readouterr().out


def test_enable_still_rejects_an_unknown_name(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    assert cli.main(["install", "-C", str(project), "--without", "beta"]) == 0
    capsys.readouterr()

    assert cli.main(["enable", "-C", str(project), "nosuchthing"]) == 1
    assert "declares no [commands.nosuchthing]" in capsys.readouterr().err


def test_disable_does_not_accept_a_stale_exclusion_name(
    isolated_env, make_project, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only `enable` widens to stored names; disabling an undeclared item is a typo."""
    project = make_project("demo")
    excludable_config(project, tmp_path / "home")
    assert cli.main(["install", "-C", str(project), "--without", "beta"]) == 0
    (project / "spg.toml").write_text(
        dedent("""\
            [project]
            name = "demo"

            [commands.alpha]
            run = "./scripts/hello.sh"
        """)
    )
    assert cli.main(["sync"]) == 0
    capsys.readouterr()

    assert cli.main(["disable", "-C", str(project), "beta"]) == 1
    assert "declares no [commands.beta]" in capsys.readouterr().err
