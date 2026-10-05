"""Recompiling: stale files drop, other variants stay, and runtime-init is generated once."""

from __future__ import annotations

from pathlib import Path

from tundravm.backends import InProcessBackend
from tundravm.declarative import (
    Declaration,
    File,
    Fragment,
    Init,
    Package,
    Recipe,
    Variant,
    lower,
)
from tundravm.declarative._compile import emit
from tundravm.declarative.lifecycle import bake_image
from tundravm.models import RecipeState

VARIANTS = ("default", "dev")


def _recipe(*items: Declaration) -> Recipe:
    return Recipe(
        "reset",
        Fragment("common", items=(Package("linux-image-amd64"), Package("curl"), *items)),
        variants=(
            Variant("default", target="qemu"),
            Variant("dev", add=Fragment("dev", items=(Package("vim"),))),
        ),
    )


def test_recompile_removes_stale_variant_files(tmp_path: Path) -> None:
    dest = tmp_path / "mkosi"
    emit(lower(_recipe(File("/etc/old.conf", "old\n"))).select(VARIANTS), dest)
    old_path = dest / "default" / "mkosi.extra" / "etc" / "old.conf"
    assert old_path.exists()
    stray = dest / "default" / "stray.txt"
    stray.write_text("left behind\n", encoding="utf-8")
    root_note = dest / "NOTES.md"
    root_note.write_text("kept\n", encoding="utf-8")

    emit(lower(_recipe()).select(VARIANTS), dest)

    assert not old_path.exists()
    assert not stray.exists()
    assert root_note.exists()
    assert (dest / "dev" / "mkosi.conf").exists()


def test_compile_of_one_variant_leaves_other_variants_alone(tmp_path: Path) -> None:
    image = lower(_recipe())
    dest = tmp_path / "mkosi"
    emit(image.select(VARIANTS), dest)
    marker = dest / "dev" / "marker"
    marker.write_text("x", encoding="utf-8")

    emit(image.select(("default",)), dest)

    assert marker.exists()
    assert (dest / "default" / "mkosi.conf").exists()


def _init_recipe(extra: Variant) -> Recipe:
    return Recipe(
        "init",
        Fragment(
            "common",
            items=(
                Init("hello", "echo hi", priority=10),
                Package("linux-image-amd64"),
                Package("curl"),
            ),
        ),
        variants=(Variant("default", target="qemu"), extra),
    )


def _init_service_count(state: RecipeState, profile: str) -> int:
    services = state.effective_profile(profile).services
    return sum(1 for svc in services if svc.name == "runtime-init.service")


def test_recompile_with_more_variants_does_not_duplicate_runtime_init(tmp_path: Path) -> None:
    img = lower(_init_recipe(Variant("dev", add=Fragment("dev", items=(Package("vim"),)))))
    payload = img.select(VARIANTS).payload()

    emit(img, tmp_path / "first")
    emit(img.select(VARIANTS), tmp_path / "second")
    emit(img, tmp_path / "third")

    # Compiling generates runtime-init into the tree only, never into the declared state.
    assert _init_service_count(img.state, "default") == 0
    assert _init_service_count(img.state, "dev") == 0
    assert img.select(VARIANTS).payload() == payload
    for tree in ("first", "second", "third"):
        assert (tmp_path / tree / "default/mkosi.extra/usr/bin/runtime-init").is_file()
    assert (tmp_path / "second/dev/mkosi.extra/usr/bin/runtime-init").is_file()


def test_bake_after_compile_across_variants(
    tmp_path: Path, inprocess_backend: InProcessBackend
) -> None:
    img = lower(_init_recipe(Variant("azure", target="azure")))

    emit(img, tmp_path / "preview")
    result, artifacts = bake_image(
        img,
        ("default", "azure"),
        locked=None,
        backend=inprocess_backend,
        out=tmp_path / "build",
    )
    assert set(result.profiles) == {"azure", "default"}
    assert {a.variant for a in artifacts} == {"azure", "default"}
