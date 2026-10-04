"""Recompiling with a different profile selection must stay idempotent."""

from __future__ import annotations

from pathlib import Path

from tundravm import Image


def _init_service_count(img: Image, profile: str) -> int:
    services = img.state.effective_profile(profile).services
    return sum(1 for svc in services if svc.name == "runtime-init.service")


def test_recompile_with_more_profiles_does_not_duplicate_runtime_init(tmp_path: Path) -> None:
    img = Image()
    img.runtime_init("echo hi", priority=10)
    img.install("curl")
    with img.profile("dev"):
        img.install("vim")

    img.compile(tmp_path / "first")
    with img.all_profiles():
        img.compile(tmp_path / "second")
    img.compile(tmp_path / "third", force=True)

    assert _init_service_count(img, "default") == 1
    assert _init_service_count(img, "dev") == 1


def test_bake_after_compile_across_profiles(tmp_path: Path, inprocess_backend: object) -> None:
    img = Image(build_dir=tmp_path / "build", backend=inprocess_backend)  # type: ignore[arg-type]
    img.runtime_init("echo hi", priority=10)
    img.install("curl")
    with img.profile("azure"):
        img.targets("azure")

    img.compile(tmp_path / "preview")
    with img.all_profiles():
        result = img.bake()
    assert set(result.profiles) == {"azure", "default"}
