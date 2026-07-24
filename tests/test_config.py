from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from spg.config import ConfigError, load_project_config_from_dir


def write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "spg.toml").write_text(body)
    return tmp_path


def test_loads_minimal_config(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"
    """),
    )
    config = load_project_config_from_dir(tmp_path)
    assert config.name == "demo"
    assert config.commands == ()
    assert config.root == tmp_path.resolve()


def test_loads_command_with_args(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [commands.greet]
        run = "./scripts/greet"
        description = "Say hi"
        args = [
            { name = "who", description = "name" },
            { name = "--loud" },
        ]
    """),
    )
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
    write(
        tmp_path,
        dedent("""\
        [project]
    """),
    )
    with pytest.raises(ConfigError, match=r"project\]\.name"):
        load_project_config_from_dir(tmp_path)


def test_command_missing_run(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [commands.bad]
        description = "no run"
    """),
    )
    with pytest.raises(ConfigError, match=r"commands\.bad\] must set 'run' or 'shell_function'"):
        load_project_config_from_dir(tmp_path)


def test_loads_shell_function_command(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [commands.gocd]
        description = "cd into a worktree"
        shell_function = 'cd "$(./scripts/resolve.sh "$@")"'
        complete_hook = "./scripts/hook.sh"
        args = [
            { name = "target" },
        ]
    """),
    )
    config = load_project_config_from_dir(tmp_path)
    cmd = config.commands[0]
    assert cmd.name == "gocd"
    assert cmd.run == ""
    assert cmd.shell_function == 'cd "$(./scripts/resolve.sh "$@")"'
    assert cmd.is_shell_function is True
    assert cmd.complete_hook == "./scripts/hook.sh"
    assert [a.name for a in cmd.args] == ["target"]


def test_command_run_and_shell_function_mutually_exclusive(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [commands.bad]
        run = "./x"
        shell_function = "cd ."
    """),
    )
    with pytest.raises(ConfigError, match="cannot set both 'run' and 'shell_function'"):
        load_project_config_from_dir(tmp_path)


def test_command_run_must_be_string(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [commands.bad]
        run = 123
    """),
    )
    with pytest.raises(ConfigError, match=r"commands\.bad\]\.run must be a string"):
        load_project_config_from_dir(tmp_path)


def test_shell_function_must_be_string(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [commands.bad]
        shell_function = 42
    """),
    )
    with pytest.raises(ConfigError, match=r"commands\.bad\]\.shell_function must be a string"):
        load_project_config_from_dir(tmp_path)


def test_invalid_command_name(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [commands."bad name"]
        run = "./x"
    """),
    )
    with pytest.raises(ConfigError, match="invalid command name"):
        load_project_config_from_dir(tmp_path)


def test_arg_must_be_table(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [commands.foo]
        run = "./x"
        args = ["bare"]
    """),
    )
    with pytest.raises(ConfigError, match=r"args\[0\]"):
        load_project_config_from_dir(tmp_path)


def test_missing_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_project_config_from_dir(tmp_path)


def test_loads_completion_fields(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
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
    """),
    )
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
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [commands.x]
        run = "./x"
        args = [{ name = "y", type = "bogus" }]
    """),
    )
    with pytest.raises(ConfigError, match=r"args\[0\].type"):
        load_project_config_from_dir(tmp_path)


@pytest.mark.parametrize(
    "bad_name",
    ["foo:bar", "has space", "-leading-dash", "1leading-digit", "has\nnewline"],
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
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [commands.x]
        run = "./x"
        args = [{ name = "y", type = "files", values = ["a"] }]
    """),
    )
    with pytest.raises(ConfigError, match="cannot set both"):
        load_project_config_from_dir(tmp_path)


# --- [links] ---------------------------------------------------------------


def test_links_default_to_empty(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"
    """),
    )
    assert load_project_config_from_dir(tmp_path).links == ()


def test_parses_links(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [links.my-skill]
        source = "skills/my-skill"
        target = "~/.claude/skills/"
        description = "Publish the skill"

        [links.rgconf]
        source = "config/ripgreprc"
        target = "~/.config/ripgrep/config"
    """),
    )
    config = load_project_config_from_dir(tmp_path)
    assert [link.name for link in config.links] == ["my-skill", "rgconf"]

    skill = config.link("my-skill")
    assert skill is not None
    assert skill.description == "Publish the skill"
    assert skill.target_is_dir is True
    # A trailing slash means "link into that directory", so the leaf is the name.
    assert skill.link_path == Path("~/.claude/skills").expanduser() / "my-skill"
    assert skill.source_path(tmp_path) == tmp_path / "skills/my-skill"

    rgconf = config.link("rgconf")
    assert rgconf is not None
    assert rgconf.target_is_dir is False
    assert rgconf.link_path == Path("~/.config/ripgrep/config").expanduser()

    assert config.link("nope") is None


def test_link_section_must_be_a_table(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        links = "nope"

        [project]
        name = "demo"
    """),
    )
    with pytest.raises(ConfigError, match=r"\[links\] must be a table"):
        load_project_config_from_dir(tmp_path)


def test_link_body_must_be_a_table(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [links]
        skill = "nope"
    """),
    )
    with pytest.raises(ConfigError, match=r"\[links.skill\] must be a table"):
        load_project_config_from_dir(tmp_path)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('target = "~/x"', "source is required"),
        ('source = ""\ntarget = "~/x"', "source is required"),
        ('source = "/abs/path"\ntarget = "~/x"', "must be a path relative"),
        ('source = "~/in-home"\ntarget = "~/x"', "must be a path relative"),
        ('source = "../escape"\ntarget = "~/x"', "must not contain"),
        ('source = "a/../../escape"\ntarget = "~/x"', "must not contain"),
        ('source = "skills/s"', "target is required"),
        ('source = "skills/s"\ntarget = ""', "target is required"),
        ('source = "skills/s"\ntarget = "relative/path"', "must be an absolute path"),
        ('source = "skills/s"\ntarget = "~/x"\ndescription = 3', "description must be a string"),
    ],
)
def test_link_validation_errors(tmp_path: Path, body: str, match: str) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [links.skill]
        """)
        + body
        + "\n",
    )
    with pytest.raises(ConfigError, match=match):
        load_project_config_from_dir(tmp_path)


@pytest.mark.parametrize("name", ["..", ".", "has/slash", "has space"])
def test_invalid_link_names(tmp_path: Path, name: str) -> None:
    write(
        tmp_path,
        dedent(f"""\
        [project]
        name = "demo"

        [links."{name}"]
        source = "skills/s"
        target = "~/x"
    """),
    )
    with pytest.raises(ConfigError, match="invalid link name"):
        load_project_config_from_dir(tmp_path)


@pytest.mark.parametrize("name", [".zshrc", "1password", "my-skill", "a.b_c"])
def test_link_names_allow_filename_shapes(tmp_path: Path, name: str) -> None:
    write(
        tmp_path,
        dedent(f"""\
        [project]
        name = "demo"

        [links."{name}"]
        source = "skills/s"
        target = "~/"
    """),
    )
    config = load_project_config_from_dir(tmp_path)
    assert config.links[0].name == name


def test_two_links_cannot_resolve_to_the_same_path(tmp_path: Path) -> None:
    write(
        tmp_path,
        dedent("""\
        [project]
        name = "demo"

        [links.skill]
        source = "a"
        target = "~/.claude/skills/"

        [links.other]
        source = "b"
        target = "~/.claude/skills/skill"
    """),
    )
    with pytest.raises(ConfigError, match="both resolve to"):
        load_project_config_from_dir(tmp_path)
