from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest


@pytest.fixture
def make_project(tmp_path: Path):
    """Create a fresh project directory with a spg.toml.

    Pass `commands` as a string (raw TOML body, sans [project] section) or
    rely on a single 'hello' command stub.
    """

    def _make(name: str = "demo", commands_toml: str | None = None) -> Path:
        project_dir = tmp_path / name
        project_dir.mkdir()
        body = dedent(
            f"""\
            [project]
            name = "{name}"
            """
        )
        if commands_toml is None:
            body += dedent(
                """\
                [commands.hello]
                run = "./scripts/hello.sh"
                description = "Say hello"
                args = [
                    { name = "who", description = "name to greet" },
                ]
                """
            )
        else:
            body += commands_toml
        (project_dir / "spg.toml").write_text(body)
        scripts = project_dir / "scripts"
        scripts.mkdir(exist_ok=True)
        hello = scripts / "hello.sh"
        hello.write_text("#!/bin/sh\necho hello \"$@\"\n")
        hello.chmod(0o755)
        return project_dir

    return _make


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    p = tmp_path / "bin"
    p.mkdir()
    return p


@pytest.fixture
def registry_file(tmp_path: Path) -> Path:
    return tmp_path / "registry.toml"
