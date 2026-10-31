from pathlib import Path

import pytest

from tundravm.declarative import (
    Artifact,
    Backend,
    Package,
    Recipe,
    Variant,
    bake,
    lock,
    measure,
    verify_artifact,
)
from tundravm.errors import ArtifactError


def _bake(tmp_path: Path, *variants: Variant) -> tuple[Artifact, ...]:
    """Bake *variants* with the Linux backend (the fake mkosi) into ``tmp_path / "build"``."""
    recipe = Recipe(
        "artifacts",
        items=(Package("curl"), Package("linux-image-amd64")),
        variants=variants,
    )
    return bake(recipe, lock=lock(recipe), backend=Backend("local"), build=tmp_path / "build")


def test_each_artifact_records_its_digests_and_boot_payload(tmp_path: Path) -> None:
    (artifact,) = _bake(tmp_path, Variant("default", outputs=("qemu",)))

    assert len(artifact.sha256) == 64 and artifact.tree_digest and artifact.lock_digest
    assert artifact.build == tmp_path / "build"
    assert artifact.boot_payload is not None and artifact.boot_payload.name == "payload.efi"
    assert artifact.boot_payload_sha256 is not None and len(artifact.boot_payload_sha256) == 64
    assert artifact.installed is None  # no source build


def test_changed_artifact_is_refused_by_measure(tmp_path: Path) -> None:
    (artifact,) = _bake(tmp_path, Variant("default", outputs=("qemu",)))
    artifact.path.write_bytes(b"swapped after the bake\n")

    for attempt in (lambda: verify_artifact(artifact), lambda: measure(artifact)):
        with pytest.raises(ArtifactError) as excinfo:
            attempt()
        assert excinfo.value.code == "E_ARTIFACT_CHANGED"
        assert excinfo.value.context["recorded"] == artifact.sha256

    artifact.path.unlink()
    with pytest.raises(ArtifactError, match="Cannot read artifact"):
        verify_artifact(artifact)
