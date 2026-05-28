from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from spg.config import ConfigError, load_project_config_from_dir


def write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "spg.toml").write_text(body)
    return tmp_path


def test_loads_minimal_config(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
        name = "demo"
    """))
    config = load_project_config_from_dir(tmp_path)
    assert config.name == "demo"
    assert config.commands == ()
    assert config.root == tmp_path.resolve()


def test_loads_command_with_args(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
        name = "demo"

        [commands.greet]
        run = "./scripts/greet"
        description = "Say hi"
        args = [
            { name = "who", description = "name" },
            { name = "--loud" },
        ]
    """))
    config = load_project_config_from_dir(tmp_path)
    assert len(config.commands) == 1
    cmd = config.commands[0]
    assert cmd.name == "greet"
    assert cmd.run == "./scripts/greet"
    assert cmd.description == "Say hi"
    assert [a.name for a in cmd.args] == ["who", "--loud"]
    assert config.command("greet") is cmd
    assert config.command("nope") is None


def test_missing_project_section(tmp_path: Path) -> None:
    write(tmp_path, "")
    with pytest.raises(ConfigError, match=r"\[project\] section"):
        load_project_config_from_dir(tmp_path)


def test_missing_project_name(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
    """))
    with pytest.raises(ConfigError, match="project].name"):
        load_project_config_from_dir(tmp_path)


def test_command_missing_run(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
        name = "demo"

        [commands.bad]
        description = "no run"
    """))
    with pytest.raises(
        ConfigError, match=r"commands\.bad\] must set 'run' or 'shell_function'"
    ):
        load_project_config_from_dir(tmp_path)


def test_loads_shell_function_command(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
        name = "demo"

        [commands.gocd]
        description = "cd into a worktree"
        shell_function = 'cd "$(./scripts/resolve.sh "$@")"'
        complete_hook = "./scripts/hook.sh"
        args = [
            { name = "target" },
        ]
    """))
    config = load_project_config_from_dir(tmp_path)
    cmd = config.commands[0]
    assert cmd.name == "gocd"
    assert cmd.run == ""
    assert cmd.shell_function == 'cd "$(./scripts/resolve.sh "$@")"'
    assert cmd.is_shell_function is True
    assert cmd.complete_hook == "./scripts/hook.sh"
    assert [a.name for a in cmd.args] == ["target"]


def test_command_run_and_shell_function_mutually_exclusive(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
        name = "demo"

        [commands.bad]
        run = "./x"
        shell_function = "cd ."
    """))
    with pytest.raises(ConfigError, match="cannot set both 'run' and 'shell_function'"):
        load_project_config_from_dir(tmp_path)


def test_command_run_must_be_string(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
        name = "demo"

        [commands.bad]
        run = 123
    """))
    with pytest.raises(ConfigError, match=r"commands\.bad\]\.run must be a string"):
        load_project_config_from_dir(tmp_path)


def test_shell_function_must_be_string(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
        name = "demo"

        [commands.bad]
        shell_function = 42
    """))
    with pytest.raises(
        ConfigError, match=r"commands\.bad\]\.shell_function must be a string"
    ):
        load_project_config_from_dir(tmp_path)


def test_invalid_command_name(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
        name = "demo"

        [commands."bad name"]
        run = "./x"
    """))
    with pytest.raises(ConfigError, match="invalid command name"):
        load_project_config_from_dir(tmp_path)


def test_arg_must_be_table(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
        name = "demo"

        [commands.foo]
        run = "./x"
        args = ["bare"]
    """))
    with pytest.raises(ConfigError, match=r"args\[0\]"):
        load_project_config_from_dir(tmp_path)


def test_missing_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_project_config_from_dir(tmp_path)


def test_loads_completion_fields(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
        name = "demo"

        [commands.deploy]
        run = "./x"
        args = [
            { name = "target", values = ["staging", "prod"] },
            { name = "config", type = "files" },
            { name = "--region", description = "region", values = ["us-east-1"] },
            { name = "--dry-run" },
        ]
        complete_hook = "./scripts/x __complete"
    """))
    config = load_project_config_from_dir(tmp_path)
    cmd = config.commands[0]
    target, cfg, region, dry = cmd.args
    assert target.values == ("staging", "prod")
    assert target.is_flag is False
    assert cfg.type == "files"
    assert region.is_flag is True
    assert region.expects_value is True
    assert dry.is_flag is True
    assert dry.expects_value is False
    assert cmd.complete_hook == "./scripts/x __complete"


def test_arg_type_must_be_known(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
        name = "demo"

        [commands.x]
        run = "./x"
        args = [{ name = "y", type = "bogus" }]
    """))
    with pytest.raises(ConfigError, match=r"args\[0\].type"):
        load_project_config_from_dir(tmp_path)


@pytest.mark.parametrize(
    "bad_name",
    ['foo:bar', 'has space', '-leading-dash', '1leading-digit', 'has\nnewline'],
)
def test_project_name_rejects_unsafe_characters(tmp_path: Path, bad_name: str) -> None:
    raw = bad_name.encode("unicode_escape").decode("ascii")
    write(
        tmp_path,
        f'[project]\nname = "{raw}"\n',
    )
    with pytest.raises(ConfigError, match=r"\[project\]\.name"):
        load_project_config_from_dir(tmp_path)


def test_project_name_allows_safe_characters(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent(
            """\
            [project]
            name = "demo_v1.2-final"
            """
        ),
    )
    config = load_project_config_from_dir(tmp_path)
    assert config.name == "demo_v1.2-final"


def test_arg_cannot_set_both_type_and_values(tmp_path: Path) -> None:
    write(tmp_path, dedent("""\
        [project]
        name = "demo"

        [commands.x]
        run = "./x"
        args = [{ name = "y", type = "files", values = ["a"] }]
    """))
    with pytest.raises(ConfigError, match="cannot set both"):
        load_project_config_from_dir(tmp_path)
