from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from textwrap import dedent

import pytest

from spg.config import load_project_config_from_dir
from spg.installer import (
    ExclusionChange,
    InstallError,
    install_project,
    list_managed_wrappers,
    prune_orphan_wrappers,
    sync_project,
    uninstall_project,
)
from spg.registry import Registry


def test_install_writes_executable_wrappers(
    make_project, bin_dir: Path, registry_file: Path
) -> None:
    project = make_project("demo")
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)

    result = install_project(config, registry, bin_dir)
    assert result.written == ["hello"]

    wrapper = bin_dir / "hello"
    assert wrapper.is_file()
    assert os.access(str(wrapper), os.X_OK)

    content = wrapper.read_text()
    assert "# spg-managed: demo:hello" in content
    assert "./scripts/hello.sh" in content
    assert str(project.resolve()) in content


def test_install_idempotent_refresh(make_project, bin_dir: Path, registry_file: Path) -> None:
    project = make_project("demo")
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)

    install_project(config, registry, bin_dir)
    result = install_project(config, registry, bin_dir)
    assert result.written == []
    assert result.refreshed == ["hello"]


def test_install_removes_orphaned_commands(
    make_project, bin_dir: Path, registry_file: Path
) -> None:
    body_two = dedent("""\
        [commands.alpha]
        run = "./scripts/hello.sh"

        [commands.beta]
        run = "./scripts/hello.sh"
    """)
    project = make_project("demo", commands_toml=body_two)
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    install_project(config, registry, bin_dir)
    assert (bin_dir / "alpha").exists()
    assert (bin_dir / "beta").exists()

    body_one = dedent("""\
        [commands.alpha]
        run = "./scripts/hello.sh"
    """)
    (project / "spg.toml").write_text('[project]\nname = "demo"\n\n' + body_one)
    config = load_project_config_from_dir(project)
    result = install_project(config, registry, bin_dir)
    assert "beta" in result.removed
    assert not (bin_dir / "beta").exists()
    assert (bin_dir / "alpha").exists()


def test_install_conflict_with_unmanaged_file(
    make_project, bin_dir: Path, registry_file: Path
) -> None:
    project = make_project("demo")
    (bin_dir / "hello").write_text("#!/bin/sh\n# not ours\n")
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)

    with pytest.raises(InstallError, match="not managed by spg"):
        install_project(config, registry, bin_dir, force=False)

    install_project(config, registry, bin_dir, force=True)
    assert "# spg-managed: demo:hello" in (bin_dir / "hello").read_text()


def test_install_conflict_with_other_project(
    make_project, bin_dir: Path, registry_file: Path
) -> None:
    project_a = make_project("alpha")
    project_b = make_project("bravo")
    registry = Registry.load(registry_file)

    install_project(load_project_config_from_dir(project_a), registry, bin_dir)
    config_b = load_project_config_from_dir(project_b)
    with pytest.raises(InstallError, match="already provided by project"):
        install_project(config_b, registry, bin_dir)


def test_uninstall_removes_only_owned_wrappers(
    make_project, bin_dir: Path, registry_file: Path
) -> None:
    project = make_project("demo")
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    install_project(config, registry, bin_dir)
    assert (bin_dir / "hello").exists()

    result = uninstall_project("demo", registry, bin_dir)
    assert result.removed == ["hello"]
    assert not (bin_dir / "hello").exists()
    assert "demo" not in Registry.load(registry_file).projects


def test_uninstall_skips_non_managed_wrappers(
    make_project, bin_dir: Path, registry_file: Path
) -> None:
    project = make_project("demo")
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    install_project(config, registry, bin_dir)

    (bin_dir / "hello").write_text("#!/bin/sh\necho rogue\n")

    result = uninstall_project("demo", registry, bin_dir)
    assert result.removed == []
    assert result.skipped == ["hello"]
    assert (bin_dir / "hello").read_text() == "#!/bin/sh\necho rogue\n"


def test_list_managed_wrappers(make_project, bin_dir: Path, registry_file: Path) -> None:
    project = make_project("demo")
    install_project(load_project_config_from_dir(project), Registry.load(registry_file), bin_dir)
    (bin_dir / "unrelated").write_text("#!/bin/sh\necho hi\n")

    found = list_managed_wrappers(bin_dir)
    names = [path.name for path, _ in found]
    assert names == ["hello"]
    assert found[0][1].project == "demo"


def test_wrapper_executes_with_args(make_project, bin_dir: Path, registry_file: Path) -> None:
    project = make_project("demo")
    install_project(load_project_config_from_dir(project), Registry.load(registry_file), bin_dir)
    result = subprocess.run(
        [str(bin_dir / "hello"), "world"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "hello world"


def test_wrapper_runs_multiword_command(tmp_path: Path, bin_dir: Path, registry_file: Path) -> None:
    project = tmp_path / "multi"
    project.mkdir()
    (project / "spg.toml").write_text(
        dedent("""\
        [project]
        name = "multi"

        [commands.q]
        run = "/usr/bin/env echo prefix"
    """)
    )
    config = load_project_config_from_dir(project)
    install_project(config, Registry.load(registry_file), bin_dir)
    result = subprocess.run(
        [str(bin_dir / "q"), "tail"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "prefix tail"


def test_wrapper_exposes_invocation_dir(tmp_path: Path, bin_dir: Path, registry_file: Path) -> None:
    """The wrapper cd's into the project root, but a command can still recover
    the directory the user invoked it from via $SPG_INVOCATION_DIR."""
    project = tmp_path / "invdir"
    project.mkdir()
    (project / "spg.toml").write_text(
        dedent("""\
        [project]
        name = "invdir"

        [commands.where]
        run = "/bin/sh -c 'echo \\"$SPG_INVOCATION_DIR\\"'"
    """)
    )
    caller = tmp_path / "caller"
    caller.mkdir()
    config = load_project_config_from_dir(project)
    install_project(config, Registry.load(registry_file), bin_dir)
    result = subprocess.run(
        [str(bin_dir / "where")],
        cwd=caller,
        capture_output=True,
        text=True,
        check=True,
    )
    assert Path(result.stdout.strip()) == caller


def test_install_refuses_symlink_in_bin_dir(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    project = make_project("demo")
    sensitive = tmp_path / "sensitive.txt"
    sensitive.write_text("important user data\n")
    (bin_dir / "hello").symlink_to(sensitive)

    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    with pytest.raises(InstallError, match="symlink"):
        install_project(config, registry, bin_dir)
    assert sensitive.read_text() == "important user data\n"
    assert (bin_dir / "hello").is_symlink()


def test_install_force_replaces_symlink_dirent_not_target(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    project = make_project("demo")
    sensitive = tmp_path / "sensitive.txt"
    sensitive.write_text("important user data\n")
    (bin_dir / "hello").symlink_to(sensitive)

    config = load_project_config_from_dir(project)
    install_project(config, Registry.load(registry_file), bin_dir, force=True)

    wrapper = bin_dir / "hello"
    assert not wrapper.is_symlink()
    assert wrapper.is_file()
    assert "# spg-managed: demo:hello" in wrapper.read_text()
    assert sensitive.read_text() == "important user data\n"


def test_install_refuses_same_name_from_different_root(
    make_project, tmp_path: Path, bin_dir: Path, registry_file: Path
) -> None:
    proj_a = make_project("dupe")
    install_project(load_project_config_from_dir(proj_a), Registry.load(registry_file), bin_dir)

    proj_b = tmp_path / "elsewhere"
    proj_b.mkdir()
    (proj_b / "spg.toml").write_text(
        dedent(
            """\
            [project]
            name = "dupe"

            [commands.world]
            run = "echo world"
            """
        )
    )

    config_b = load_project_config_from_dir(proj_b)
    with pytest.raises(InstallError, match="already installed from"):
        install_project(config_b, Registry.load(registry_file), bin_dir)

    # Original wrapper untouched, points at the original root.
    wrapper = bin_dir / "hello"
    assert str(proj_a.resolve()) in wrapper.read_text()


def test_install_allows_reinstall_from_same_root(
    make_project, bin_dir: Path, registry_file: Path
) -> None:
    project = make_project("demo")
    config = load_project_config_from_dir(project)
    install_project(config, Registry.load(registry_file), bin_dir)
    # Second install from the same root must still succeed.
    install_project(config, Registry.load(registry_file), bin_dir)


def test_uninstall_skips_symlink_wrapper(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    project = make_project("demo")
    config = load_project_config_from_dir(project)
    install_project(config, Registry.load(registry_file), bin_dir)

    # User replaces the wrapper with a symlink to a sensitive file.
    sensitive = tmp_path / "sensitive.txt"
    sensitive.write_text("important user data\n")
    wrapper = bin_dir / "hello"
    wrapper.unlink()
    wrapper.symlink_to(sensitive)

    result = uninstall_project("demo", Registry.load(registry_file), bin_dir)
    assert result.removed == []
    assert result.skipped == ["hello"]
    # The symlink target must not be removed or rewritten.
    assert sensitive.read_text() == "important user data\n"


def test_concurrent_installs_serialize(
    make_project, tmp_path: Path, bin_dir: Path, registry_file: Path
) -> None:
    proj_a = make_project("alpha")
    proj_b = tmp_path / "beta"
    proj_b.mkdir()
    (proj_b / "spg.toml").write_text(
        dedent(
            """\
            [project]
            name = "beta"

            [commands.beta]
            run = "echo beta"
            """
        )
    )
    config_a = load_project_config_from_dir(proj_a)
    config_b = load_project_config_from_dir(proj_b)

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def _install(config) -> None:
        barrier.wait()
        try:
            install_project(config, Registry.load(registry_file), bin_dir)
        except Exception as exc:  # pragma: no cover - reported through assertion
            errors.append(exc)

    t1 = threading.Thread(target=_install, args=(config_a,))
    t2 = threading.Thread(target=_install, args=(config_b,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == []
    final = Registry.load(registry_file)
    assert set(final.projects.keys()) == {"alpha", "beta"}


def test_install_shell_function_skips_wrapper_but_registers(
    tmp_path: Path, bin_dir: Path, registry_file: Path
) -> None:
    project = tmp_path / "fn"
    project.mkdir()
    (project / "spg.toml").write_text(
        dedent("""\
        [project]
        name = "fn"

        [commands.gocd]
        description = "cd somewhere"
        shell_function = 'cd "$(echo "$@")"'
    """)
    )
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    result = install_project(config, registry, bin_dir)
    # No wrapper written
    assert result.written == []
    assert result.refreshed == []
    assert not (bin_dir / "gocd").exists()
    # But it IS registered (so list/uninstall/sync see it)
    final = Registry.load(registry_file)
    assert final.projects["fn"].commands == ("gocd",)


def test_install_mixed_run_and_shell_function(
    tmp_path: Path, bin_dir: Path, registry_file: Path
) -> None:
    project = tmp_path / "mixed"
    project.mkdir()
    scripts = project / "scripts"
    scripts.mkdir()
    (scripts / "hi.sh").write_text("#!/bin/sh\necho hi\n")
    (scripts / "hi.sh").chmod(0o755)
    (project / "spg.toml").write_text(
        dedent("""\
        [project]
        name = "mixed"

        [commands.hi]
        run = "./scripts/hi.sh"

        [commands.gocd]
        shell_function = 'cd .'
    """)
    )
    config = load_project_config_from_dir(project)
    result = install_project(config, Registry.load(registry_file), bin_dir)
    assert result.written == ["hi"]
    assert (bin_dir / "hi").exists()
    assert not (bin_dir / "gocd").exists()
    assert set(Registry.load(registry_file).projects["mixed"].commands) == {"hi", "gocd"}


def test_install_switch_run_to_shell_function_removes_wrapper(
    tmp_path: Path, bin_dir: Path, registry_file: Path
) -> None:
    project = tmp_path / "switch"
    project.mkdir()
    scripts = project / "scripts"
    scripts.mkdir()
    (scripts / "hi.sh").write_text("#!/bin/sh\necho hi\n")
    (scripts / "hi.sh").chmod(0o755)
    spg_toml = project / "spg.toml"
    spg_toml.write_text(
        dedent("""\
        [project]
        name = "switch"

        [commands.foo]
        run = "./scripts/hi.sh"
    """)
    )
    install_project(load_project_config_from_dir(project), Registry.load(registry_file), bin_dir)
    assert (bin_dir / "foo").exists()

    spg_toml.write_text(
        dedent("""\
        [project]
        name = "switch"

        [commands.foo]
        shell_function = 'cd .'
    """)
    )
    result = install_project(
        load_project_config_from_dir(project), Registry.load(registry_file), bin_dir
    )
    assert "foo" in result.removed
    assert not (bin_dir / "foo").exists()


def test_install_shell_function_collides_across_projects(
    tmp_path: Path, bin_dir: Path, registry_file: Path
) -> None:
    project_a = tmp_path / "a"
    project_a.mkdir()
    (project_a / "spg.toml").write_text(
        dedent("""\
        [project]
        name = "a"

        [commands.shared]
        shell_function = 'cd .'
    """)
    )
    install_project(load_project_config_from_dir(project_a), Registry.load(registry_file), bin_dir)

    project_b = tmp_path / "b"
    project_b.mkdir()
    (project_b / "spg.toml").write_text(
        dedent("""\
        [project]
        name = "b"

        [commands.shared]
        shell_function = 'cd /tmp'
    """)
    )
    with pytest.raises(InstallError, match="already registered to project 'a'"):
        install_project(
            load_project_config_from_dir(project_b), Registry.load(registry_file), bin_dir
        )


def test_uninstall_shell_function_command_silently(
    tmp_path: Path, bin_dir: Path, registry_file: Path
) -> None:
    project = tmp_path / "fn"
    project.mkdir()
    (project / "spg.toml").write_text(
        dedent("""\
        [project]
        name = "fn"

        [commands.gocd]
        shell_function = 'cd .'
    """)
    )
    config = load_project_config_from_dir(project)
    install_project(config, Registry.load(registry_file), bin_dir)
    result = uninstall_project("fn", Registry.load(registry_file), bin_dir)
    # No file ever existed; should not be reported as skipped.
    assert result.removed == []
    assert result.skipped == []


def _write_managed_wrapper(bin_dir: Path, name: str, *, project: str, command: str) -> None:
    (bin_dir / name).write_text(f"#!/bin/sh\n# spg-managed: {project}:{command}\nexec true\n")


def test_prune_removes_wrapper_from_unregistered_project(
    make_project, bin_dir: Path, registry_file: Path
) -> None:
    project = make_project("demo")
    install_project(load_project_config_from_dir(project), Registry.load(registry_file), bin_dir)
    _write_managed_wrapper(bin_dir, "ghost", project="phantom", command="ghost")

    result = prune_orphan_wrappers(Registry.load(registry_file), bin_dir)
    assert result.removed == ["ghost"]
    assert not (bin_dir / "ghost").exists()
    assert (bin_dir / "hello").exists()


def test_prune_removes_wrapper_not_in_registry_entry(
    make_project, bin_dir: Path, registry_file: Path
) -> None:
    project = make_project("demo")
    install_project(load_project_config_from_dir(project), Registry.load(registry_file), bin_dir)
    # A wrapper claiming "demo" but never recorded in its registry entry
    # (e.g. left behind by an interrupted install).
    _write_managed_wrapper(bin_dir, "stale", project="demo", command="stale")

    result = prune_orphan_wrappers(Registry.load(registry_file), bin_dir)
    assert result.removed == ["stale"]
    assert not (bin_dir / "stale").exists()
    assert (bin_dir / "hello").exists()


def test_prune_respects_skip_projects(make_project, bin_dir: Path, registry_file: Path) -> None:
    project = make_project("demo")
    install_project(load_project_config_from_dir(project), Registry.load(registry_file), bin_dir)
    _write_managed_wrapper(bin_dir, "ghost", project="phantom", command="ghost")

    result = prune_orphan_wrappers(Registry.load(registry_file), bin_dir, skip_projects={"phantom"})
    assert result.removed == []
    assert (bin_dir / "ghost").exists()


def test_prune_ignores_unmanaged_files(make_project, bin_dir: Path, registry_file: Path) -> None:
    project = make_project("demo")
    install_project(load_project_config_from_dir(project), Registry.load(registry_file), bin_dir)
    (bin_dir / "unrelated").write_text("#!/bin/sh\necho hi\n")

    result = prune_orphan_wrappers(Registry.load(registry_file), bin_dir)
    assert result.removed == []
    assert (bin_dir / "unrelated").exists()


def test_wrapper_quotes_root_with_space(tmp_path: Path, bin_dir: Path, registry_file: Path) -> None:
    project = tmp_path / "has space"
    project.mkdir()
    scripts = project / "scripts"
    scripts.mkdir()
    hello = scripts / "hello.sh"
    hello.write_text('#!/bin/sh\necho yes "$@"\n')
    hello.chmod(0o755)
    (project / "spg.toml").write_text(
        dedent("""\
        [project]
        name = "spaced"

        [commands.s]
        run = "./scripts/hello.sh"
    """)
    )
    config = load_project_config_from_dir(project)
    install_project(config, Registry.load(registry_file), bin_dir)
    result = subprocess.run(
        [str(bin_dir / "s"), "ok"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "yes ok"


# --- [links] ---------------------------------------------------------------


def link_project(make_project, name: str = "demo", links_toml: str = "") -> Path:
    """A project with one wrapper command plus the given [links] tables."""
    project = make_project(
        name,
        dedent("""\
            [commands.hello]
            run = "./scripts/hello.sh"
        """)
        + links_toml,
    )
    skill = project / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# skill\n")
    (project / "note.txt").write_text("hi\n")
    return project


def test_install_creates_links(make_project, bin_dir: Path, registry_file: Path, tmp_path: Path):
    home = tmp_path / "home"
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.my-skill]
            source = "skills/my-skill"
            target = "{home}/.claude/skills/"

            [links.note]
            source = "note.txt"
            target = "{home}/note-link.txt"
        """),
    )
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)

    result = install_project(config, registry, bin_dir)
    assert result.links_written == ["my-skill", "note"]
    assert result.links_relinked == []

    # Parent directories are created on demand.
    skill_link = home / ".claude" / "skills" / "my-skill"
    assert skill_link.is_symlink()
    assert skill_link.readlink() == project.resolve() / "skills" / "my-skill"
    assert (skill_link / "SKILL.md").read_text() == "# skill\n"

    note_link = home / "note-link.txt"
    assert note_link.is_symlink()
    assert note_link.read_text() == "hi\n"

    entry = Registry.load(registry_file).projects["demo"]
    assert [link.name for link in entry.links] == ["my-skill", "note"]
    assert entry.link(skill_link) is not None


def test_install_links_are_idempotent(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
):
    home = tmp_path / "home"
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.my-skill]
            source = "skills/my-skill"
            target = "{home}/.claude/skills/"
        """),
    )
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)

    install_project(config, registry, bin_dir)
    result = install_project(config, registry, bin_dir)
    assert result.links_written == []
    assert result.links_relinked == []
    assert (home / ".claude" / "skills" / "my-skill").is_symlink()


def test_install_repoints_our_own_stale_link(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
):
    home = tmp_path / "home"
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.thing]
            source = "skills/my-skill"
            target = "{home}/thing"
        """),
    )
    registry = Registry.load(registry_file)
    install_project(load_project_config_from_dir(project), registry, bin_dir)

    # Someone repoints our link by hand; sync/install puts it back.
    link = home / "thing"
    link.unlink()
    link.symlink_to(project / "note.txt")

    result = install_project(load_project_config_from_dir(project), registry, bin_dir)
    assert result.links_relinked == ["thing"]
    assert link.readlink() == project.resolve() / "skills" / "my-skill"


def test_install_accepts_equivalent_existing_link(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
):
    """A hand-made link that already resolves to the source is not a conflict.

    Here the existing link is *relative*, so its value differs from the absolute
    path spg writes while pointing at the same place. spg canonicalizes it
    instead of reporting a foreign-symlink conflict.
    """
    home = tmp_path / "home"
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.thing]
            source = "skills/my-skill"
            target = "{home}/thing"
        """),
    )
    source = project.resolve() / "skills" / "my-skill"
    home.mkdir(parents=True)
    relative = Path(os.path.relpath(source, home))
    assert str(relative).startswith("..")
    (home / "thing").symlink_to(relative)

    result = install_project(
        load_project_config_from_dir(project), Registry.load(registry_file), bin_dir
    )
    assert result.links_relinked == ["thing"]
    assert (home / "thing").readlink() == source


def test_install_refuses_foreign_symlink_without_force(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
):
    home = tmp_path / "home"
    home.mkdir()
    (home / "thing").symlink_to(tmp_path / "somewhere-else")
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.thing]
            source = "skills/my-skill"
            target = "{home}/thing"
        """),
    )
    config = load_project_config_from_dir(project)

    with pytest.raises(InstallError, match="refusing to repoint it"):
        install_project(config, Registry.load(registry_file), bin_dir)

    result = install_project(config, Registry.load(registry_file), bin_dir, force=True)
    assert result.links_relinked == ["thing"]
    assert (home / "thing").readlink() == project.resolve() / "skills" / "my-skill"


def test_install_refuses_regular_file_without_force(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
):
    home = tmp_path / "home"
    home.mkdir()
    (home / "thing").write_text("precious\n")
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.thing]
            source = "skills/my-skill"
            target = "{home}/thing"
        """),
    )
    config = load_project_config_from_dir(project)

    with pytest.raises(InstallError, match="not managed by spg"):
        install_project(config, Registry.load(registry_file), bin_dir)
    assert (home / "thing").read_text() == "precious\n"

    install_project(config, Registry.load(registry_file), bin_dir, force=True)
    assert (home / "thing").is_symlink()


def test_install_never_replaces_a_real_directory(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
):
    home = tmp_path / "home"
    (home / "thing").mkdir(parents=True)
    (home / "thing" / "keep.txt").write_text("keep\n")
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.thing]
            source = "skills/my-skill"
            target = "{home}/thing"
        """),
    )
    config = load_project_config_from_dir(project)

    # Not even --force may delete a real directory.
    for force in (False, True):
        with pytest.raises(InstallError, match="is a directory; refusing to replace it"):
            install_project(config, Registry.load(registry_file), bin_dir, force=force)
    assert (home / "thing" / "keep.txt").read_text() == "keep\n"


def test_directory_conflict_hints_at_trailing_slash(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.my-skill]
            source = "skills/my-skill"
            target = "{home}/skills"
        """),
    )
    config = load_project_config_from_dir(project)
    with pytest.raises(InstallError, match=r"\[links.my-skill\].target"):
        install_project(config, Registry.load(registry_file), bin_dir)


def test_install_rejects_missing_source(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
):
    home = tmp_path / "home"
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.typo]
            source = "skils/my-skill"
            target = "{home}/x"
        """),
    )
    config = load_project_config_from_dir(project)
    with pytest.raises(InstallError, match="does not exist"):
        install_project(config, Registry.load(registry_file), bin_dir)


def test_link_cannot_collide_with_another_projects_link(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
):
    home = tmp_path / "home"
    links = dedent(f"""\
        [links.shared]
        source = "skills/my-skill"
        target = "{home}/shared"
    """)
    first = link_project(make_project, "one", links)
    second = link_project(make_project, "two", links)
    registry = Registry.load(registry_file)

    install_project(load_project_config_from_dir(first), registry, bin_dir)
    with pytest.raises(InstallError, match="already published by project 'one'"):
        install_project(load_project_config_from_dir(second), registry, bin_dir)


def test_link_cannot_collide_with_own_wrapper(make_project, bin_dir: Path, registry_file: Path):
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.hello]
            source = "skills/my-skill"
            target = "{bin_dir}/hello"
        """),
    )
    config = load_project_config_from_dir(project)
    with pytest.raises(InstallError, match="also the wrapper path"):
        install_project(config, Registry.load(registry_file), bin_dir)


def test_link_refuses_to_replace_another_projects_wrapper(
    make_project, bin_dir: Path, registry_file: Path
):
    other = make_project("other", '[commands.othercmd]\nrun = "./scripts/hello.sh"\n')
    registry = Registry.load(registry_file)
    install_project(load_project_config_from_dir(other), registry, bin_dir)

    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.othercmd]
            source = "skills/my-skill"
            target = "{bin_dir}/othercmd"
        """),
    )
    with pytest.raises(InstallError, match="is the spg wrapper for command"):
        install_project(load_project_config_from_dir(project), registry, bin_dir)


def test_install_removes_links_no_longer_declared(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
):
    home = tmp_path / "home"
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.keep]
            source = "skills/my-skill"
            target = "{home}/keep"

            [links.drop]
            source = "note.txt"
            target = "{home}/drop"
        """),
    )
    registry = Registry.load(registry_file)
    install_project(load_project_config_from_dir(project), registry, bin_dir)
    assert (home / "drop").is_symlink()

    (project / "spg.toml").write_text(
        dedent(f"""\
            [project]
            name = "demo"

            [commands.hello]
            run = "./scripts/hello.sh"

            [links.keep]
            source = "skills/my-skill"
            target = "{home}/keep"
        """)
    )
    result = install_project(load_project_config_from_dir(project), registry, bin_dir)
    assert result.links_removed == ["drop"]
    assert not (home / "drop").exists()
    assert (home / "keep").is_symlink()

    entry = Registry.load(registry_file).projects["demo"]
    assert [link.name for link in entry.links] == ["keep"]


def test_uninstall_removes_links(make_project, bin_dir: Path, registry_file: Path, tmp_path: Path):
    home = tmp_path / "home"
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.my-skill]
            source = "skills/my-skill"
            target = "{home}/.claude/skills/"
        """),
    )
    registry = Registry.load(registry_file)
    install_project(load_project_config_from_dir(project), registry, bin_dir)
    link = home / ".claude" / "skills" / "my-skill"
    assert link.is_symlink()

    result = uninstall_project("demo", registry, bin_dir)
    assert result.links_removed == ["my-skill"]
    assert not link.exists()
    # The linked-to content is untouched.
    assert (project / "skills" / "my-skill" / "SKILL.md").exists()
    # And the directory we created for it stays put.
    assert link.parent.is_dir()


def test_uninstall_keeps_paths_that_are_no_longer_symlinks(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
):
    home = tmp_path / "home"
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.thing]
            source = "skills/my-skill"
            target = "{home}/thing"
        """),
    )
    registry = Registry.load(registry_file)
    install_project(load_project_config_from_dir(project), registry, bin_dir)

    # Someone replaced our symlink with real content.
    (home / "thing").unlink()
    (home / "thing").write_text("mine now\n")

    result = uninstall_project("demo", registry, bin_dir)
    assert result.links_removed == []
    assert result.links_skipped == ["thing"]
    assert (home / "thing").read_text() == "mine now\n"


def test_link_write_is_atomic_and_leaves_no_temp_files(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
):
    home = tmp_path / "home"
    project = link_project(
        make_project,
        links_toml=dedent(f"""\
            [links.thing]
            source = "skills/my-skill"
            target = "{home}/thing"
        """),
    )
    install_project(load_project_config_from_dir(project), Registry.load(registry_file), bin_dir)
    assert [p.name for p in home.iterdir()] == ["thing"]


# --- user exclusions --------------------------------------------------------


def excludable_project(make_project, tmp_path: Path, name: str = "demo") -> Path:
    """A project with two wrapper commands and one link, for decline tests."""
    home = tmp_path / "home"
    project = make_project(
        name,
        dedent(f"""\
            [commands.alpha]
            run = "./scripts/hello.sh"
            description = "Alpha"

            [commands.beta]
            run = "./scripts/hello.sh"
            description = "Beta"

            [links.my-skill]
            source = "skills/my-skill"
            target = "{home}/.claude/skills/"
        """),
    )
    skill = project / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# skill\n")
    return project


def skill_link(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".claude" / "skills" / "my-skill"


def test_install_without_command_writes_no_wrapper(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    project = excludable_project(make_project, tmp_path)
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)

    result = install_project(
        config,
        registry,
        bin_dir,
        changes=ExclusionChange(disable_commands=("beta",)),
    )
    assert result.written == ["alpha"]
    assert result.excluded == ["cmd:beta"]
    assert (bin_dir / "alpha").exists()
    assert not (bin_dir / "beta").exists()

    entry = Registry.load(registry_file).projects["demo"]
    assert entry.commands == ("alpha",)
    assert entry.excluded_commands == ("beta",)


def test_install_without_link_creates_no_symlink(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    project = excludable_project(make_project, tmp_path)
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)

    result = install_project(
        config,
        registry,
        bin_dir,
        changes=ExclusionChange(disable_links=("my-skill",)),
    )
    assert result.links_written == []
    assert result.excluded == ["link:my-skill"]
    assert not skill_link(tmp_path).exists()

    entry = Registry.load(registry_file).projects["demo"]
    assert entry.links == ()
    assert entry.excluded_links == ("my-skill",)


def test_sync_keeps_an_exclusion(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    """The regression this whole feature exists for: sync must not undo a decline.

    `sync` re-reads spg.toml and reconciles in both directions, so without
    persisted exclusions it would recreate the wrapper and the symlink.
    """
    project = excludable_project(make_project, tmp_path)
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    install_project(
        config,
        registry,
        bin_dir,
        changes=ExclusionChange(disable_commands=("beta",), disable_links=("my-skill",)),
    )

    result = sync_project(load_project_config_from_dir(project), registry, bin_dir)

    assert not (bin_dir / "beta").exists()
    assert not skill_link(tmp_path).exists()
    assert (bin_dir / "alpha").exists()
    assert result.excluded == ["cmd:beta", "link:my-skill"]
    entry = Registry.load(registry_file).projects["demo"]
    assert entry.commands == ("alpha",)
    assert entry.excluded_commands == ("beta",)
    assert entry.excluded_links == ("my-skill",)


def test_disable_then_enable_a_command(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    project = excludable_project(make_project, tmp_path)
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    install_project(config, registry, bin_dir)
    assert (bin_dir / "beta").exists()

    disabled = sync_project(
        config, registry, bin_dir, changes=ExclusionChange(disable_commands=("beta",))
    )
    assert disabled.removed == ["beta"]
    assert not (bin_dir / "beta").exists()

    enabled = sync_project(
        config, registry, bin_dir, changes=ExclusionChange(enable_commands=("beta",))
    )
    assert enabled.written == ["beta"]
    assert (bin_dir / "beta").exists()
    assert enabled.excluded == []
    assert Registry.load(registry_file).projects["demo"].excluded_commands == ()


def test_disable_then_enable_a_link(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    project = excludable_project(make_project, tmp_path)
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    install_project(config, registry, bin_dir)
    assert skill_link(tmp_path).is_symlink()

    disabled = sync_project(
        config, registry, bin_dir, changes=ExclusionChange(disable_links=("my-skill",))
    )
    assert disabled.links_removed == ["my-skill"]
    assert not skill_link(tmp_path).exists()
    assert Registry.load(registry_file).projects["demo"].links == ()

    enabled = sync_project(
        config, registry, bin_dir, changes=ExclusionChange(enable_links=("my-skill",))
    )
    assert enabled.links_written == ["my-skill"]
    assert skill_link(tmp_path).is_symlink()


def test_declined_command_does_not_conflict_with_a_foreign_file(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    """Declining is the escape hatch for a name that collides with something you own."""
    project = excludable_project(make_project, tmp_path)
    (bin_dir / "beta").write_text("#!/bin/sh\n# mine, not spg's\n")
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)

    with pytest.raises(InstallError, match="not managed by spg"):
        install_project(config, registry, bin_dir)

    result = install_project(
        config, registry, bin_dir, changes=ExclusionChange(disable_commands=("beta",))
    )
    assert result.written == ["alpha"]
    # The user's own file is left exactly as it was.
    assert (bin_dir / "beta").read_text() == "#!/bin/sh\n# mine, not spg's\n"


def test_declined_link_does_not_conflict_with_a_foreign_file(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    project = excludable_project(make_project, tmp_path)
    link_path = skill_link(tmp_path)
    link_path.parent.mkdir(parents=True)
    link_path.write_text("mine\n")
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)

    with pytest.raises(InstallError, match="not managed by spg"):
        install_project(config, registry, bin_dir)

    install_project(config, registry, bin_dir, changes=ExclusionChange(disable_links=("my-skill",)))
    assert link_path.read_text() == "mine\n"


def test_uninstall_forgets_exclusions(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    project = excludable_project(make_project, tmp_path)
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    install_project(config, registry, bin_dir, changes=ExclusionChange(disable_commands=("beta",)))
    uninstall_project("demo", registry, bin_dir)

    result = install_project(config, registry, bin_dir)
    assert result.excluded == []
    assert (bin_dir / "beta").exists()
    assert Registry.load(registry_file).projects["demo"].excluded_commands == ()


def test_stored_exclusion_for_removed_command_does_not_break_sync(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    """A stale exclusion is kept, not pruned, and never fails a sync."""
    project = excludable_project(make_project, tmp_path)
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    install_project(config, registry, bin_dir, changes=ExclusionChange(disable_commands=("beta",)))

    # Upstream drops the command the user had declined.
    (project / "spg.toml").write_text(
        dedent("""\
            [project]
            name = "demo"

            [commands.alpha]
            run = "./scripts/hello.sh"
        """)
    )
    result = sync_project(load_project_config_from_dir(project), registry, bin_dir)
    assert result.excluded == ["cmd:beta"]
    entry = Registry.load(registry_file).projects["demo"]
    assert entry.commands == ("alpha",)
    assert entry.excluded_commands == ("beta",)


def test_declined_shell_function_leaves_no_wrapper_and_no_registry_entry(
    tmp_path: Path, bin_dir: Path, registry_file: Path
) -> None:
    project = tmp_path / "fn"
    project.mkdir()
    (project / "spg.toml").write_text(
        dedent("""\
        [project]
        name = "fn"

        [commands.gocd]
        shell_function = 'cd .'

        [commands.gohome]
        shell_function = 'cd ~'
    """)
    )
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    install_project(config, registry, bin_dir, changes=ExclusionChange(disable_commands=("gocd",)))

    assert not (bin_dir / "gocd").exists()
    entry = Registry.load(registry_file).projects["fn"]
    assert entry.commands == ("gohome",)
    assert entry.excluded_commands == ("gocd",)


def test_exclusions_are_sorted_deduped_and_union_then_subtract(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    project = excludable_project(make_project, tmp_path)
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    install_project(
        config,
        registry,
        bin_dir,
        changes=ExclusionChange(disable_commands=("beta", "beta", "alpha")),
    )
    assert Registry.load(registry_file).projects["demo"].excluded_commands == ("alpha", "beta")

    # Union-then-subtract: enabling wins over a stored exclusion in the same call.
    sync_project(
        config,
        registry,
        bin_dir,
        changes=ExclusionChange(disable_commands=("beta",), enable_commands=("alpha", "beta")),
    )
    assert Registry.load(registry_file).projects["demo"].excluded_commands == ()


def test_plain_reinstall_keeps_an_exclusion(
    make_project, bin_dir: Path, registry_file: Path, tmp_path: Path
) -> None:
    """A bare `spg install` re-run must not silently re-enable a decline.

    Nothing in the invocation says "install everything", so the stored set is
    carried forward exactly as `sync` carries it.
    """
    project = excludable_project(make_project, tmp_path)
    config = load_project_config_from_dir(project)
    registry = Registry.load(registry_file)
    install_project(
        config,
        registry,
        bin_dir,
        changes=ExclusionChange(disable_commands=("beta",), disable_links=("my-skill",)),
    )

    result = install_project(load_project_config_from_dir(project), registry, bin_dir)

    assert result.excluded == ["cmd:beta", "link:my-skill"]
    assert not (bin_dir / "beta").exists()
    assert not skill_link(tmp_path).exists()
    entry = Registry.load(registry_file).projects["demo"]
    assert entry.excluded_commands == ("beta",)
    assert entry.excluded_links == ("my-skill",)
