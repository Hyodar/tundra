import hashlib
from collections.abc import Sequence
from pathlib import Path

from tundravm.declarative import (
    Artifact,
    Backend,
    Fragment,
    Hook,
    Package,
    Recipe,
    Variant,
    bake,
    lock,
)


def _recipe() -> Recipe:
    return Recipe(
        "repro",
        Fragment("common", items=(Package("curl"), Hook("hello", "prepare", "echo hello"))),
        variants=(Variant("default", targets=("qemu", "azure")),),
    )


def _bake(out: Path) -> tuple[Artifact, ...]:
    recipe = _recipe()
    return bake(recipe, locked=lock(recipe), backend=Backend("inprocess"), out=out)


def test_repeated_bakes_with_same_recipe_have_stable_artifact_digests(tmp_path: Path) -> None:
    first = _bake(tmp_path / "build-a")
    second = _bake(tmp_path / "build-b")

    assert _artifact_digest_map(first, variant="default") == _artifact_digest_map(
        second,
        variant="default",
    )
    assert set(_artifact_digest_map(first, variant="default")) == {"azure", "qemu"}


def _artifact_digest_map(artifacts: Sequence[Artifact], *, variant: str) -> dict[str, str]:
    digests: dict[str, str] = {}
    for artifact in sorted(artifacts, key=lambda a: a.target):
        if artifact.variant != variant:
            continue
        digest = hashlib.sha256(artifact.path.read_bytes()).hexdigest()
        assert digest == artifact.sha256
        digests[artifact.target] = digest
    return digests
