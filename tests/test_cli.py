from __future__ import annotations

from pathlib import Path

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


def test_init_creates_starter_config(isolated_env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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


def test_init_refuses_overwrite(isolated_env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "newproj"
    target.mkdir()
    (target / "spg.toml").write_text("[project]\nname = \"x\"\n")
    rc = cli.main(["init", "-C", str(target)])
    assert rc == 1


def test_install_list_uninstall_flow(isolated_env, make_project, capsys: pytest.CaptureFixture[str]) -> None:
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


def test_help_for_registered_command(isolated_env, make_project, capsys: pytest.CaptureFixture[str]) -> None:
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


def test_status_clean(isolated_env, make_project, capsys: pytest.CaptureFixture[str]) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()

    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "All good" in out


def test_status_flags_orphan(isolated_env, make_project, capsys: pytest.CaptureFixture[str]) -> None:
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


def test_sync_warns_when_config_missing(isolated_env, make_project, capsys: pytest.CaptureFixture[str]) -> None:
    project = make_project("demo")
    cli.main(["install", "-C", str(project)])
    capsys.readouterr()

    (project / "spg.toml").unlink()
    rc = cli.main(["sync"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "is missing" in err
