"""``Image.directory()``: import a host directory tree into mkosi.extra."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm import Image, ValidationError


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


def test_imports_nested_tree_under_dest(tmp_path: Path) -> None:
    src = _tree(tmp_path / "etc")
    img = Image().directory("/etc/app/", src=src)

    files = {f.path: f for f in img.state.profiles["default"].files}
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
    img = Image().directory("/opt", src=src)
    modes = {f.path: f.mode for f in img.state.profiles["default"].files}
    assert modes["/opt/bin/start.sh"] == "0755"
    assert modes["/opt/motd"] == "0644"

    forced = Image().directory("/opt", src=src, mode="0600")
    assert {f.mode for f in forced.state.profiles["default"].files} == {"0600"}


def test_exclude_globs_match_relative_paths_and_prune_dirs(tmp_path: Path) -> None:
    src = _tree(tmp_path / "etc")
    img = Image().directory("/etc", src=src, exclude=("cache", "*.swp", "systemd/*/*.network"))

    assert [f.path for f in img.state.profiles["default"].files] == [
        "/etc/bin/start.sh",
        "/etc/motd",
    ]


def test_order_is_deterministic(tmp_path: Path) -> None:
    src = _tree(tmp_path / "etc")
    first = [f.path for f in Image().directory("/x", src=src).state.profiles["default"].files]
    second = [f.path for f in Image().directory("/x", src=src).state.profiles["default"].files]
    assert first == second == sorted(first)


def test_binary_files_are_emitted_byte_for_byte(tmp_path: Path) -> None:
    src = tmp_path / "blobs"
    src.mkdir()
    blob = bytes(range(256))
    (src / "firmware.bin").write_bytes(blob)
    img = Image(reproducible=False).directory("/lib/firmware", src=src)

    entry = img.state.profiles["default"].files[0]
    assert entry.content == blob
    img.compile(tmp_path / "tree")
    emitted = tmp_path / "tree" / "default" / "mkosi.extra" / "lib" / "firmware" / "firmware.bin"
    assert emitted.read_bytes() == blob
    assert "firmware.bin" in img.summary()


def test_scoped_to_profile(tmp_path: Path) -> None:
    src = _tree(tmp_path / "etc")
    img = Image()
    img.profile("dev").directory("/etc/dev", src=src, exclude=("cache",))
    assert img.state.profiles["default"].files == []
    assert len(img.state.profiles["dev"].files) == 4


def test_rejects_missing_or_empty_src(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="existing directory"):
        Image().directory("/etc", src=tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValidationError, match="no files"):
        Image().directory("/etc", src=tmp_path / "empty")
