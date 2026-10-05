"""``Directory``: import a host directory tree into mkosi.extra."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import run_main
from tundravm.declarative import Directory, Fragment, Recipe, Variant, compile, lower
from tundravm.errors import ValidationError
from tundravm.models import FileEntry


def _tree(root: Path) -> Path:
    (root / "systemd" / "network").mkdir(parents=True)
    (root / "systemd" / "network" / "10-eth.network").write_text("[Match]\n", encoding="utf-8")
    (root / "motd").write_text("hello\n", encoding="utf-8")
    (root / "motd").chmod(0o644)
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


def test_preserves_modes_unless_mode_given(tmp_path: Path) -> None:
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


def _private_tree(root: Path) -> Path:
    """A 0600 file, a symlink to it, a dangling link and an empty 0700 directory."""
    root.mkdir()
    (root / "token").write_text("secret\n", encoding="utf-8")
    (root / "token").chmod(0o600)
    (root / "current").symlink_to("token")
    (root / "gone").symlink_to("/nowhere")
    (root / "spool").mkdir()
    (root / "spool").chmod(0o700)
    return root


def test_keeps_modes_symlinks_and_empty_directories(tmp_path: Path) -> None:
    src = _private_tree(tmp_path / "app")
    entries = {f.path: f for f in _files(_recipe(Directory("/etc/app", src)))}

    assert entries["/etc/app/token"] == FileEntry("/etc/app/token", "secret\n", "0600")
    assert entries["/etc/app/current"] == FileEntry("/etc/app/current", "token", "0777", "symlink")
    assert entries["/etc/app/gone"].kind == "symlink"
    assert entries["/etc/app/spool"] == FileEntry("/etc/app/spool", b"", "0700", "directory")

    tree = {
        e.path.removeprefix("default/mkosi.extra"): e
        for e in compile(_recipe(Directory("/etc/app", src))).entries
    }
    assert tree["/etc/app/token"].mode == 0o600
    assert tree["/etc/app/current"].symlink == "token"
    assert tree["/etc/app/gone"].symlink == "/nowhere"
    assert tree["/etc/app/spool"].content is None and tree["/etc/app/spool"].mode == 0o700


def test_mode_normalises_files_and_empty_directories(tmp_path: Path) -> None:
    src = _private_tree(tmp_path / "app")
    entries = {f.path: f for f in _files(_recipe(Directory("/etc/app", src, mode=0o640)))}
    assert entries["/etc/app/token"].mode == "0640"
    assert entries["/etc/app/spool"].mode == "0755"
    assert entries["/etc/app/current"].kind == "symlink"


def test_follow_reads_through_links(tmp_path: Path) -> None:
    src = _private_tree(tmp_path / "app")
    (src / "gone").unlink()
    entries = {f.path: f for f in _files(_recipe(Directory("/etc/app", src, symlinks="follow")))}
    assert entries["/etc/app/current"] == FileEntry("/etc/app/current", "secret\n", "0600")

    (src / "gone").symlink_to("/nowhere")
    with pytest.raises(ValidationError, match="points to nothing"):
        lower(_recipe(Directory("/etc/app", src, symlinks="follow")))


def test_rejects_unknown_symlink_policy(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="symlinks"):
        Directory("/etc", tmp_path, symlinks="copy")  # type: ignore[arg-type]


def test_inspect_lists_modes_links_and_directories(tmp_path: Path) -> None:
    _private_tree(tmp_path / "app")
    recipe = tmp_path / "image.py"
    recipe.write_text(
        "from pathlib import Path\n"
        "from tundravm import Directory, Fragment, Package, Recipe\n"
        "recipe = Recipe('t', Fragment('c', items=(Package('linux-image-amd64'), "
        "Directory('/etc/app', Path(__file__).parent / 'app'))))\n",
        encoding="utf-8",
    )
    code, out = run_main("inspect", str(recipe))
    assert code == 0
    lines = {line.split()[0]: line.split()[1:] for line in out.splitlines() if "/etc/app/" in line}
    assert lines["/etc/app/token"][0] == "0600"
    assert lines["/etc/app/current"] == ["0777", "->", "token"]
    assert lines["/etc/app/spool"] == ["0700", "directory"]
