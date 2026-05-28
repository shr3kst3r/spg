from __future__ import annotations

from pathlib import Path

from spg.registry import Registry


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
