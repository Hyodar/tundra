from pathlib import Path

import pytest

from tundravm.declarative import (
    Artifact,
    Backend,
    Package,
    Qemu,
    Recipe,
    Secrets,
    Variant,
    bake,
    deploy,
    lock,
    measure,
    read_artifacts,
    verify_artifact,
)
from tundravm.deploy import qemu as qemu_mod
from tundravm.errors import ArtifactError
from tundravm.models import DeployRequest, DeployResult


def _bake(tmp_path: Path, *variants: Variant) -> tuple[Artifact, ...]:
    """Bake *variants* with the in-process backend into ``tmp_path / "build"``."""
    recipe = Recipe(
        "deploy",
        items=(Package("curl"), Package("linux-image-amd64")),
        variants=variants,
    )
    return bake(recipe, lock=lock(recipe), backend=Backend("inprocess"), build=tmp_path / "build")


def _mock_qemu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the QEMU adapter so deploying needs no QEMU."""

    def mock_deploy(self: object, request: DeployRequest) -> DeployResult:
        return DeployResult(
            target="qemu",
            deployment_id=f"qemu-{request.profile}",
            endpoint="ssh://localhost:2222",
            metadata={
                "artifact_path": str(request.artifact_path),
                **dict(request.parameters),
            },
        )

    monkeypatch.setattr(qemu_mod.QemuDeployAdapter, "deploy", mock_deploy)


def test_bake_respects_global_and_variant_output_targets(tmp_path: Path) -> None:
    artifacts = _bake(
        tmp_path,
        Variant("default", outputs=("qemu",)),
        Variant("azure", outputs=("azure",)),
        Variant("gcp", outputs=("gcp",)),
    )

    by_variant = {(a.variant, a.target): a for a in artifacts}

    assert set(by_variant) == {("default", "qemu"), ("azure", "azure"), ("gcp", "gcp")}
    assert by_variant["default", "qemu"].path.name == "disk.qcow2"
    assert by_variant["azure", "azure"].path.name == "disk.vhd"
    assert by_variant["gcp", "gcp"].path.name == "disk.raw.tar.gz"
    assert all(a.path.is_file() and a.simulated for a in artifacts)


def test_verify_artifact_passes_for_untouched_bytes(tmp_path: Path) -> None:
    (artifact,) = _bake(tmp_path, Variant("default", outputs=("qemu",)))
    verify_artifact(artifact)


def test_changed_artifact_is_refused_by_measure_and_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (artifact,) = _bake(tmp_path, Variant("default", outputs=("qemu",)))
    _mock_qemu(monkeypatch)
    artifact.path.write_bytes(b"swapped after the bake\n")

    for attempt in (
        lambda: verify_artifact(artifact),
        lambda: measure(artifact, allow_placeholder=True),
        lambda: deploy(artifact, using=Qemu(), allow_simulated=True),
    ):
        with pytest.raises(ArtifactError) as excinfo:
            attempt()
        assert excinfo.value.code == "E_ARTIFACT_CHANGED"
        assert excinfo.value.context["recorded"] == artifact.sha256

    artifact.path.unlink()
    with pytest.raises(ArtifactError, match="Cannot read artifact"):
        verify_artifact(artifact)


def test_deploy_returns_result_when_target_was_baked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (artifact,) = _bake(tmp_path, Variant("default", outputs=("qemu",)))
    _mock_qemu(monkeypatch)

    result = deploy(artifact, using=Qemu(memory="4G"), allow_simulated=True)
    metadata = dict(result.metadata)
    artifact_path = Path(metadata["artifact_path"])

    assert result.target == "qemu"
    assert result.id == "qemu-default"
    assert result.endpoint == "ssh://localhost:2222"
    assert artifact_path.exists()
    assert metadata["memory"] == "4G"


def test_deploy_forwards_the_secrets_ports_the_bake_recorded(tmp_path: Path) -> None:
    kernel = Package("linux-image-amd64")
    recipe = Recipe(
        "deploy",
        items=(kernel, Secrets(port=8443)),
        variants=(Variant("default", outputs=("qemu",)), Variant("azure", outputs=("azure",))),
    )
    locked = lock(recipe, resolver=lambda source: "a" * 40)
    artifacts = bake(recipe, lock=locked, backend=Backend("inprocess"), build=tmp_path / "build")
    ports = {a.variant: a.ports for a in artifacts}
    assert ports == {"default": (8443,), "azure": (8443,)}  # one payload: the same secrets
    assert read_artifacts(tmp_path / "build") == artifacts
    plain = Recipe("plain", items=(kernel,), variants=(Variant("default", outputs=("qemu",)),))
    (bare,) = bake(plain, lock=lock(plain), backend=Backend("inprocess"), build=tmp_path / "plain")
    assert bare.ports == ()

    calls: list[DeployRequest] = []

    class Recording:
        name = "qemu"

        def deploy(self, request: DeployRequest) -> DeployResult:
            calls.append(request)
            return DeployResult(target="qemu", deployment_id="qemu-x", endpoint=None)

    default = next(a for a in artifacts if a.variant == "default")
    deploy(default, using=Qemu(forward=((9000, 9000),)), allow_simulated=True, adapter=Recording())
    deploy(default, using=Qemu(forward=((18443, 8443),)), allow_simulated=True, adapter=Recording())
    assert [r.parameters["forward"] for r in calls] == ["9000:9000,8443:8443", "18443:8443"]
