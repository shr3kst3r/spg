from __future__ import annotations

from pathlib import Path

import pytest

from spg.registry import (
    REGISTRY_VERSION,
    Registry,
    RegistryError,
    RegistryLink,
)


def test_load_missing_file_is_empty(registry_file: Path) -> None:
    reg = Registry.load(registry_file)
    assert reg.projects == {}


def test_upsert_and_save_roundtrip(registry_file: Path, tmp_path: Path) -> None:
    reg = Registry.load(registry_file)
    project_root = tmp_path / "myproj"
    project_root.mkdir()
    reg.upsert("myproj", project_root, ("alpha", "beta"))
    reg.save()

    assert registry_file.exists()
    text = registry_file.read_text()
    assert "[projects.myproj]" in text
    assert str(project_root.resolve()) in text
    assert '"alpha"' in text and '"beta"' in text

    reloaded = Registry.load(registry_file)
    assert "myproj" in reloaded.projects
    entry = reloaded.projects["myproj"]
    assert entry.root == project_root.resolve()
    assert entry.commands == ("alpha", "beta")


def test_find_helpers(registry_file: Path, tmp_path: Path) -> None:
    reg = Registry.load(registry_file)
    root = tmp_path / "p"
    root.mkdir()
    reg.upsert("p", root, ("xx",))
    assert reg.find_by_root(root).name == "p"
    assert reg.find_owner_of_command("xx").name == "p"
    assert reg.find_owner_of_command("nope") is None


def test_remove(registry_file: Path, tmp_path: Path) -> None:
    reg = Registry.load(registry_file)
    reg.upsert("p", tmp_path, ("xx",))
    reg.remove("p")
    assert reg.projects == {}


def test_quoted_key_for_dotted_name(registry_file: Path, tmp_path: Path) -> None:
    reg = Registry.load(registry_file)
    reg.upsert("scope.thing", tmp_path, ())
    reg.save()
    text = registry_file.read_text()
    assert '[projects."scope.thing"]' in text

    reloaded = Registry.load(registry_file)
    assert "scope.thing" in reloaded.projects


def test_links_roundtrip(registry_file: Path, tmp_path: Path) -> None:
    reg = Registry.load(registry_file)
    root = tmp_path / "p"
    root.mkdir()
    links = (
        RegistryLink(name="my-skill", path=tmp_path / "home/.claude/skills/my-skill"),
        RegistryLink(name="rgconf", path=tmp_path / "home/.config/ripgrep/config"),
    )
    reg.upsert("p", root, ("cmd",), links=links)
    reg.save()

    text = registry_file.read_text()
    assert 'name = "my-skill"' in text
    assert str(tmp_path / "home/.claude/skills/my-skill") in text

    entry = Registry.load(registry_file).projects["p"]
    assert entry.links == links
    assert entry.link(tmp_path / "home/.config/ripgrep/config").name == "rgconf"
    assert entry.link(tmp_path / "nope") is None


def test_links_key_omitted_when_empty(registry_file: Path, tmp_path: Path) -> None:
    """Link-free projects serialize exactly as they did before links existed."""
    reg = Registry.load(registry_file)
    reg.upsert("p", tmp_path, ("cmd",))
    reg.save()
    lines = registry_file.read_text().splitlines()
    assert not [line for line in lines if line.startswith("links")]


def test_registry_without_links_loads(registry_file: Path, tmp_path: Path) -> None:
    """A registry written by an older spg (no version, no links) still loads."""
    registry_file.write_text(
        f'[projects.p]\nroot = "{tmp_path}"\ncommands = ["a"]\ninstalled_at = "2024-01-01"\n'
    )
    entry = Registry.load(registry_file).projects["p"]
    assert entry.commands == ("a",)
    assert entry.links == ()


def test_find_owner_of_link(registry_file: Path, tmp_path: Path) -> None:
    reg = Registry.load(registry_file)
    link_path = tmp_path / "home/thing"
    reg.upsert("p", tmp_path, (), links=(RegistryLink(name="thing", path=link_path),))
    assert reg.find_owner_of_link(link_path).name == "p"
    assert reg.find_owner_of_link(tmp_path / "other") is None


def test_save_stamps_format_version(registry_file: Path, tmp_path: Path) -> None:
    reg = Registry.load(registry_file)
    reg.upsert("p", tmp_path, ())
    reg.save()
    assert f"version = {REGISTRY_VERSION}\n" in registry_file.read_text()


def test_rejects_newer_registry_version(registry_file: Path, tmp_path: Path) -> None:
    registry_file.write_text(f"version = {REGISTRY_VERSION + 1}\n")
    with pytest.raises(RegistryError, match="newer than this spg understands"):
        Registry.load(registry_file)


def test_rejects_non_integer_version(registry_file: Path) -> None:
    registry_file.write_text('version = "1"\n')
    with pytest.raises(RegistryError, match="version must be an integer"):
        Registry.load(registry_file)


def test_rejects_malformed_links(registry_file: Path, tmp_path: Path) -> None:
    base = f'[projects.p]\nroot = "{tmp_path}"\ncommands = []\ninstalled_at = "x"\n'
    registry_file.write_text(base + 'links = "nope"\n')
    with pytest.raises(RegistryError, match="links must be a list of tables"):
        Registry.load(registry_file)

    registry_file.write_text(base + 'links = [{ name = "a" }]\n')
    with pytest.raises(RegistryError, match="need string 'name' and 'path'"):
        Registry.load(registry_file)
