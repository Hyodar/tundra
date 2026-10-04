"""``Directory``: import a host directory tree into mkosi.extra."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm.declarative import Directory, Fragment, Recipe, Variant, compile, lower
from tundravm.errors import ValidationError
from tundravm.models import FileEntry


def _tree(root: Path) -> Path:
    (root / "systemd" / "network").mkdir(parents=True)
    (root / "systemd" / "network" / "10-eth.network").write_text("[Match]\n", encoding="utf-8")
    (root / "motd").write_text("hello\n", encoding="utf-8")
    script = root / "bin" / "start.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\necho start\n", encoding="utf-8")
    script.chmod(0o755)
    (root / "cache").mkdir()
    (root / "cache" / "junk.txt").write_text("junk\n", encoding="utf-8")
    (root / "notes.swp").write_text("swap\n", encoding="utf-8")
    return root


def _recipe(*directories: Directory, epoch: int | None = 0) -> Recipe:
    return Recipe("tree", Fragment("common", items=directories), epoch=epoch)


def _files(recipe: Recipe, variant: str = "default") -> list[FileEntry]:
    return lower(recipe).state.profiles[variant].files


def test_imports_nested_tree_under_dest(tmp_path: Path) -> None:
    src = _tree(tmp_path / "etc")
    files = {f.path: f for f in _files(_recipe(Directory("/etc/app/", src)))}

    assert sorted(files) == [
        "/etc/app/bin/start.sh",
        "/etc/app/cache/junk.txt",
        "/etc/app/motd",
        "/etc/app/notes.swp",
        "/etc/app/systemd/network/10-eth.network",
    ]
    assert files["/etc/app/motd"].content == "hello\n"


def test_preserves_exec_bit_unless_mode_given(tmp_path: Path) -> None:
    src = _tree(tmp_path / "etc")
    modes = {f.path: f.mode for f in _files(_recipe(Directory("/opt", src)))}
    assert modes["/opt/bin/start.sh"] == "0755"
    assert modes["/opt/motd"] == "0644"

    forced = _files(_recipe(Directory("/opt", src, mode=0o600)))
    assert {f.mode for f in forced} == {"0600"}


def test_exclude_globs_match_relative_paths_and_prune_dirs(tmp_path: Path) -> None:
    src = _tree(tmp_path / "etc")
    recipe = _recipe(Directory("/etc", src, exclude=("cache", "*.swp", "systemd/*/*.network")))

    assert [f.path for f in _files(recipe)] == ["/etc/bin/start.sh", "/etc/motd"]


def test_order_is_deterministic(tmp_path: Path) -> None:
    src = _tree(tmp_path / "etc")
    first = [f.path for f in _files(_recipe(Directory("/x", src)))]
    second = [f.path for f in _files(_recipe(Directory("/x", src)))]
    assert first == second == sorted(first)


def test_binary_files_are_emitted_byte_for_byte(tmp_path: Path) -> None:
    src = tmp_path / "blobs"
    src.mkdir()
    blob = bytes(range(256))
    (src / "firmware.bin").write_bytes(blob)
    recipe = _recipe(Directory("/lib/firmware", src), epoch=None)

    image = lower(recipe)
    assert image.state.profiles["default"].files[0].content == blob
    compile(recipe).write(tmp_path / "tree")
    emitted = tmp_path / "tree" / "default" / "mkosi.extra" / "lib" / "firmware" / "firmware.bin"
    assert emitted.read_bytes() == blob


def test_scoped_to_variant(tmp_path: Path) -> None:
    src = _tree(tmp_path / "etc")
    recipe = Recipe(
        "tree",
        Fragment("common"),
        variants=(
            Variant("default", target="qemu"),
            Variant(
                "dev",
                add=Fragment("dev", items=(Directory("/etc/dev", src, exclude=("cache",)),)),
            ),
        ),
    )
    image = lower(recipe)
    assert image.state.profiles["default"].files == []
    assert len(image.state.profiles["dev"].files) == 4


def test_rejects_missing_or_empty_src(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="existing directory"):
        lower(_recipe(Directory("/etc", tmp_path / "missing")))
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValidationError, match="no files"):
        lower(_recipe(Directory("/etc", tmp_path / "empty")))
