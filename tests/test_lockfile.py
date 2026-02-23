from pathlib import Path

import pytest

from tdx import Image
from tdx.errors import LockfileError
from tdx.lockfile import (
    LockedFetch,
    build_lockfile,
    parse_lockfile,
    read_lockfile,
    serialize_lockfile,
)


def test_lockfile_roundtrip_parser_serializer() -> None:
    lock = build_lockfile(
        recipe={
            "base": "debian/bookworm",
            "profiles": {"default": {"packages": ["curl"]}},
        },
        fetches=[LockedFetch(source="https://example.invalid/a", kind="http", digest="abc")],
    )
    encoded = serialize_lockfile(lock)
    decoded = parse_lockfile(encoded)

    assert decoded == lock


def test_image_lock_writes_dependency_and_recipe_metadata(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build", backend="inprocess")
    image.install("curl")
    with image.profile("dev"):
        image.install("jq")

    with image.all_profiles():
        lock_path = image.lock()

    lock = read_lockfile(lock_path)
    assert lock.version == 1
    assert lock.recipe["base"] == "debian/bookworm"
    assert lock.dependencies["default"] == ["curl"]
    assert lock.dependencies["dev"] == ["jq"]
    assert lock.recipe_digest


def test_bake_frozen_fails_when_lock_missing(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build", backend="inprocess")
    with pytest.raises(LockfileError):
        image.bake(frozen=True)


def test_bake_frozen_fails_when_lock_is_stale(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build", backend="inprocess")
    image.install("curl")
    image.lock()
    image.install("jq")

    with pytest.raises(LockfileError) as excinfo:
        image.bake(frozen=True)

    assert "stale" in str(excinfo.value).lower()


def test_bake_frozen_succeeds_with_current_lock(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build", backend="inprocess")
    image.install("curl")
    image.lock()

    result = image.bake(frozen=True)
    artifact = result.artifact_for(profile="default", target="qemu")

    assert artifact is not None
