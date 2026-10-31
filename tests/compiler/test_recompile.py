"""Recompiling: stale files drop, what the tree does not own stays, runtime-init is generated
once."""

from __future__ import annotations

from pathlib import Path

from tundravm.backends import LocalLinuxBackend
from tundravm.declarative import (
    BootStep,
    Declaration,
    File,
    Fragment,
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
        items=(Package("linux-image-amd64"), Package("curl"), *items),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("dev", add=Fragment("dev", items=(Package("vim"),))),
        ),
    )


def test_recompile_removes_stale_variant_files(tmp_path: Path) -> None:
    dest = tmp_path / "mkosi"
    emit(lower(_recipe(File("/etc/old.conf", "old\n"))).select(VARIANTS), dest)
    old_path = dest / "payload" / "mkosi.extra" / "etc" / "old.conf"
    assert old_path.exists()
    stray = dest / "payload" / "stray.txt"
    stray.write_text("left behind\n", encoding="utf-8")
    root_note = dest / "NOTES.md"
    root_note.write_text("kept\n", encoding="utf-8")
    state = dest / "mkosi.tools"
    state.mkdir()
    old_layout = dest / "default"  # a per-variant directory an earlier tundravm wrote
    old_layout.mkdir()
    (old_layout / "mkosi.conf").write_text("[Output]\nFormat=uki\n", encoding="utf-8")

    emit(lower(_recipe()).select(VARIANTS), dest)

    assert not old_path.exists()
    assert not stray.exists()
    assert not old_layout.exists()
    assert root_note.exists() and state.is_dir()  # not the tree's, mkosi's state
    assert (dest / "payload" / "mkosi.profiles" / "dev" / "mkosi.conf").exists()


def test_compile_of_one_variant_writes_only_its_profile(tmp_path: Path) -> None:
    image = lower(_recipe())
    dest = tmp_path / "mkosi"
    emit(image.select(VARIANTS), dest)
    assert (dest / "payload" / "mkosi.profiles" / "dev").is_dir()

    emit(image.select(("default",)), dest)

    assert not (dest / "payload" / "mkosi.profiles").exists()
    assert (dest / "mkosi.conf").exists() and (dest / "payload" / "mkosi.conf").exists()


def _init_recipe(extra: Variant) -> Recipe:
    return Recipe(
        "init",
        items=(
            BootStep("hello", "echo hi"),
            Package("linux-image-amd64"),
            Package("curl"),
        ),
        variants=(Variant("default", outputs=("qemu",)), extra),
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
        assert (tmp_path / tree / "payload/mkosi.extra/usr/lib/runtime-init/hello").is_file()
    # the dev profile inherits the payload's runtime-init: its overlay carries none
    overlay = tmp_path / "second/payload/mkosi.profiles/dev"
    assert overlay.is_dir() and not (overlay / "mkosi.extra/usr/lib/runtime-init").exists()


def test_bake_after_compile_across_variants(tmp_path: Path) -> None:
    img = lower(_init_recipe(Variant("azure", outputs=("azure",))))

    emit(img, tmp_path / "preview")
    result, artifacts = bake_image(
        img,
        ("default", "azure"),
        locked=None,
        backend=LocalLinuxBackend(),
        build=tmp_path / "build",
    )
    assert set(result.profiles) == {"azure", "default"}
    assert {a.variant for a in artifacts} == {"azure", "default"}
