"""Recompiling with a different variant selection must stay idempotent."""

from __future__ import annotations

from pathlib import Path

from tundravm.backends import InProcessBackend
from tundravm.declarative import Fragment, Init, Package, Recipe, Variant, lower
from tundravm.declarative.lifecycle import bake_image
from tundravm.models import RecipeState


def _recipe(extra: Variant) -> Recipe:
    return Recipe(
        "init",
        Fragment("common", items=(Init("hello", "echo hi", priority=10), Package("curl"))),
        variants=(Variant("default", target="qemu"), extra),
    )


def _init_service_count(state: RecipeState, profile: str) -> int:
    services = state.effective_profile(profile).services
    return sum(1 for svc in services if svc.name == "runtime-init.service")


def test_recompile_with_more_profiles_does_not_duplicate_runtime_init(tmp_path: Path) -> None:
    img = lower(_recipe(Variant("dev", add=Fragment("dev", items=(Package("vim"),)))))
    payload = img._recipe_payload(profile_names=("default", "dev"))

    img.compile(tmp_path / "first")
    img.compile(tmp_path / "second", profiles=("default", "dev"))
    img.compile(tmp_path / "third", force=True)

    # Compiling generates runtime-init into the tree only, never into the declared state.
    assert _init_service_count(img.state, "default") == 0
    assert _init_service_count(img.state, "dev") == 0
    assert img._recipe_payload(profile_names=("default", "dev")) == payload
    for tree in ("first", "second", "third"):
        assert (tmp_path / tree / "default/mkosi.extra/usr/bin/runtime-init").is_file()
    assert (tmp_path / "second/dev/mkosi.extra/usr/bin/runtime-init").is_file()


def test_bake_after_compile_across_profiles(
    tmp_path: Path, inprocess_backend: InProcessBackend
) -> None:
    img = lower(_recipe(Variant("azure", target="azure")))

    img.compile(tmp_path / "preview")
    result, artifacts = bake_image(
        img,
        ("default", "azure"),
        locked=None,
        backend=inprocess_backend,
        out=tmp_path / "build",
    )
    assert set(result.profiles) == {"azure", "default"}
    assert {a.variant for a in artifacts} == {"azure", "default"}
