import json
from pathlib import Path

from tdx import Image


def test_profile_scope_isolates_declarations() -> None:
    image = Image()

    with image.profile("dev"):
        image.install("htop")

    assert "htop" in image.state.profiles["dev"].packages
    assert "htop" not in image.state.profiles["default"].packages


def test_profiles_scope_broadcasts_declarations() -> None:
    image = Image()

    with image.profiles("dev", "azure"):
        image.install("curl")

    assert "curl" in image.state.profiles["dev"].packages
    assert "curl" in image.state.profiles["azure"].packages
    assert "curl" not in image.state.profiles["default"].packages


def test_all_profiles_scope_applies_to_every_profile() -> None:
    image = Image()
    with image.profile("dev"):
        image.install("strace")
    with image.profile("azure"):
        image.install("curl")

    with image.all_profiles():
        image.install("ca-certificates")

    assert "ca-certificates" in image.state.profiles["default"].packages
    assert "ca-certificates" in image.state.profiles["dev"].packages
    assert "ca-certificates" in image.state.profiles["azure"].packages


def test_scoped_operations_only_emit_for_selected_profiles(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build")
    with image.profile("dev"):
        image.output_targets("azure")
    with image.profile("prod"):
        image.output_targets("gcp")

    with image.profiles("dev", "prod"):
        bake_result = image.bake()
        lock_path = image.lock(tmp_path / "scoped.lock")

    assert set(bake_result.profiles) == {"dev", "prod"}
    assert not (tmp_path / "build" / "default").exists()

    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    assert sorted(payload["recipe"]["profiles"].keys()) == ["dev", "prod"]
