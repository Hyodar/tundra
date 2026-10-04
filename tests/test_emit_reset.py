"""Recompiling must drop files that are no longer part of the recipe."""

from __future__ import annotations

from pathlib import Path

from tundravm import Image


def test_recompile_removes_stale_profile_files(tmp_path: Path) -> None:
    img = Image()
    img.install("curl")
    img.file("/etc/old.conf", content="old\n")
    with img.profile("dev"):
        img.install("vim")
    dest = tmp_path / "mkosi"
    with img.all_profiles():
        img.compile(dest)
    old_path = dest / "default" / "mkosi.extra" / "etc" / "old.conf"
    assert old_path.exists()
    stray = dest / "default" / "stray.txt"
    stray.write_text("left behind\n", encoding="utf-8")
    root_note = dest / "NOTES.md"
    root_note.write_text("kept\n", encoding="utf-8")

    fresh = Image()
    fresh.install("curl")
    with fresh.profile("dev"):
        fresh.install("vim")
    with fresh.all_profiles():
        fresh.compile(dest)

    assert not old_path.exists()
    assert not stray.exists()
    assert root_note.exists()
    assert (dest / "dev" / "mkosi.conf").exists()


def test_compile_of_one_profile_leaves_other_profiles_alone(tmp_path: Path) -> None:
    img = Image()
    img.install("curl")
    with img.profile("dev"):
        img.install("vim")
    dest = tmp_path / "mkosi"
    with img.all_profiles():
        img.compile(dest)
    marker = dest / "dev" / "marker"
    marker.write_text("x", encoding="utf-8")

    img.compile(dest, force=True)

    assert marker.exists()
    assert (dest / "default" / "mkosi.conf").exists()
