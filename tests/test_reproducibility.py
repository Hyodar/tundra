import hashlib
from pathlib import Path

from tdx import Image
from tdx.backends import InProcessBackend
from tdx.models import BakeResult


def test_repeated_bakes_with_same_recipe_have_stable_artifact_digests(tmp_path: Path) -> None:
    first = Image(build_dir=tmp_path / "build-a", backend=InProcessBackend())
    first.install("curl")
    first.output_targets("qemu", "azure")
    first.run("echo hello", phase="prepare")
    first_result = first.bake()

    second = Image(build_dir=tmp_path / "build-b", backend=InProcessBackend())
    second.install("curl")
    second.output_targets("qemu", "azure")
    second.run("echo hello", phase="prepare")
    second_result = second.bake()

    assert _artifact_digest_map(first_result, profile="default") == _artifact_digest_map(
        second_result,
        profile="default",
    )


def _artifact_digest_map(result: BakeResult, *, profile: str) -> dict[str, str]:
    profile_result = result.profiles[profile]
    digests: dict[str, str] = {}
    for target, artifact in sorted(profile_result.artifacts.items()):
        payload = Path(artifact.path).read_bytes()
        digests[target] = hashlib.sha256(payload).hexdigest()
    return digests
