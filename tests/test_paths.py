from __future__ import annotations

from pathlib import Path

import pytest

from spg.paths import (
    bin_dir,
    find_project_config,
    home,
    invocation_dir,
    registry_path,
    resolve_path,
)


def test_home_and_dirs() -> None:
    assert home() == Path("~").expanduser()
    assert bin_dir() == home() / "bin"
    assert registry_path().name == "registry.toml"


def test_invocation_dir_defaults_to_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    assert invocation_dir() == tmp_path.resolve()


def test_invocation_dir_honors_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "other"
    target.mkdir()
    monkeypatch.setenv("SPG_INVOCATION_DIR", str(target))
    assert invocation_dir() == target


def test_resolve_path_relative(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "inv"
    target.mkdir()
    monkeypatch.setenv("SPG_INVOCATION_DIR", str(target))
    assert resolve_path(".") == target.resolve()
    assert resolve_path("sub") == (target / "sub").resolve()


def test_resolve_path_absolute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "inv"
    target.mkdir()
    monkeypatch.setenv("SPG_INVOCATION_DIR", str(target))
    abs_path = (tmp_path / "absolute").resolve()
    assert resolve_path(abs_path) == abs_path


def test_find_project_config(tmp_path: Path) -> None:
    project = tmp_path / "nested" / "proj"
    project.mkdir(parents=True)
    config = project / "spg.toml"
    config.write_text("[project]\nname = 'p'\n")
    sub = project / "a" / "b"
    sub.mkdir(parents=True)

    assert find_project_config(sub) == config
    assert find_project_config(tmp_path) is None
