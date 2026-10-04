"""Recompiling must drop files that are no longer part of the recipe."""

from __future__ import annotations

from pathlib import Path

from tundravm.declarative import (
    Declaration,
    File,
    Fragment,
    Package,
    Recipe,
    Variant,
    lower,
)

VARIANTS = ("default", "dev")


def _recipe(*items: Declaration) -> Recipe:
    return Recipe(
        "reset",
        Fragment("common", items=(Package("curl"), *items)),
        variants=(
            Variant("default", target="qemu"),
            Variant("dev", add=Fragment("dev", items=(Package("vim"),))),
        ),
    )


def test_recompile_removes_stale_profile_files(tmp_path: Path) -> None:
    dest = tmp_path / "mkosi"
    lower(_recipe(File("/etc/old.conf", "old\n"))).compile(dest, profiles=VARIANTS)
    old_path = dest / "default" / "mkosi.extra" / "etc" / "old.conf"
    assert old_path.exists()
    stray = dest / "default" / "stray.txt"
    stray.write_text("left behind\n", encoding="utf-8")
    root_note = dest / "NOTES.md"
    root_note.write_text("kept\n", encoding="utf-8")

    lower(_recipe()).compile(dest, profiles=VARIANTS)

    assert not old_path.exists()
    assert not stray.exists()
    assert root_note.exists()
    assert (dest / "dev" / "mkosi.conf").exists()


def test_compile_of_one_profile_leaves_other_profiles_alone(tmp_path: Path) -> None:
    image = lower(_recipe())
    dest = tmp_path / "mkosi"
    image.compile(dest, profiles=VARIANTS)
    marker = dest / "dev" / "marker"
    marker.write_text("x", encoding="utf-8")

    image.compile(dest, force=True, profiles=("default",))

    assert marker.exists()
    assert (dest / "default" / "mkosi.conf").exists()
