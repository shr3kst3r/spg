from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from textwrap import dedent

import pytest

from spg.config import load_project_config_from_dir
from spg.installer import (
    InstallError,
    install_project,
    list_managed_wrappers,
    prune_orphan_wrappers,
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
