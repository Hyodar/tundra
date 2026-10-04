"""``BakeResult`` persistence: ``bake-result.json`` round trip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tundravm import Image, StateError
from tundravm.backends import InProcessBackend
from tundravm.models import BAKE_RESULT_FILENAME, ArtifactRef, BakeResult, ProfileBuildResult


def _result(build_dir: Path, outside: Path) -> BakeResult:
    default = ProfileBuildResult(
        profile="default",
        artifacts={
            "qemu": ArtifactRef(target="qemu", path=build_dir / "default" / "disk.qcow2"),
            "azure": ArtifactRef(target="azure", path=outside / "disk.vhd", digest="ab" * 32),
        },
        report_path=build_dir / "default" / "report.json",
    )
    return BakeResult(
        profiles={"default": default},
        lock_digest="cd" * 32,
        backend="inprocess",
        created_at="2026-10-04T12:00:00+00:00",
    )


def test_round_trip_stores_relative_paths_inside_build_dir(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    outside = tmp_path / "elsewhere"
    original = _result(build_dir, outside)

    path = original.save(build_dir)

    assert path == build_dir / BAKE_RESULT_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    entry = payload["profiles"]["default"]
    assert entry["artifacts"]["qemu"] == "default/disk.qcow2"
    assert entry["report_path"] == "default/report.json"
    assert entry["artifacts"]["azure"] == str(outside.resolve() / "disk.vhd")
    assert payload["backend"] == "inprocess"
    assert payload["lock_digest"] == "cd" * 32
    assert payload["created_at"] == "2026-10-04T12:00:00+00:00"

    assert BakeResult.load(build_dir) == original


def test_relative_paths_resolve_against_the_load_location(tmp_path: Path) -> None:
    build_dir = tmp_path / "checkout-a" / "build"
    _result(build_dir, tmp_path).save(build_dir)
    moved = tmp_path / "checkout-b" / "build"
    moved.parent.mkdir()
    build_dir.rename(moved)

    loaded = BakeResult.load(moved)

    artifact = loaded.artifact_for(profile="default", target="qemu")
    assert artifact is not None
    assert artifact.path == moved / "default" / "disk.qcow2"


def test_load_missing_file_raises_state_error(tmp_path: Path) -> None:
    with pytest.raises(StateError) as excinfo:
        BakeResult.load(tmp_path)
    assert excinfo.value.code == "E_STATE"
    assert excinfo.value.hint == "Run bake() / tundravm bake first."


def test_load_corrupt_file_raises_state_error(tmp_path: Path) -> None:
    (tmp_path / BAKE_RESULT_FILENAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError, match="Unreadable bake result"):
        BakeResult.load(tmp_path)


def test_bake_saves_and_fresh_image_measures_and_finds_it(tmp_path: Path) -> None:
    def make() -> Image:
        img = Image(build_dir=tmp_path / "build", backend=InProcessBackend())
        img.output_targets("qemu")
        return img

    baked = make().bake()
    assert (tmp_path / "build" / BAKE_RESULT_FILENAME).is_file()
    assert baked.backend == "inprocess"

    fresh = make()
    assert fresh.last_bake() == baked
    assert fresh.measure(backend="rtmr").values
