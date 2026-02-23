from pathlib import Path

import pytest

from tdx import Image
from tdx.errors import DeploymentError, ValidationError


def _image_with_backend(tmp_path: Path) -> Image:
    """Create an Image with the inprocess backend for testing."""
    img = Image(build_dir=tmp_path / "build", backend="inprocess")
    return img


def test_bake_respects_global_and_profile_output_targets(tmp_path: Path) -> None:
    image = _image_with_backend(tmp_path)
    image.output_targets("qemu")
    with image.profile("azure"):
        image.output_targets("azure")
    with image.profile("gcp"):
        image.output_targets("gcp")

    with image.all_profiles():
        result = image.bake()

    default_qemu = result.artifact_for(profile="default", target="qemu")
    azure_vhd = result.artifact_for(profile="azure", target="azure")
    gcp_raw = result.artifact_for(profile="gcp", target="gcp")

    assert default_qemu is not None
    assert azure_vhd is not None
    assert gcp_raw is not None
    assert default_qemu.path.name == "disk.qcow2"
    assert azure_vhd.path.name == "disk.vhd"
    assert gcp_raw.path.name == "disk.raw.tar.gz"
    assert result.artifact_for(profile="default", target="azure") is None


def test_deploy_fails_when_target_artifact_not_baked(tmp_path: Path) -> None:
    image = _image_with_backend(tmp_path)
    image.output_targets("qemu")
    image.bake()

    with pytest.raises(DeploymentError) as excinfo:
        image.deploy(target="azure")

    assert "not baked" in str(excinfo.value).lower()
    assert excinfo.value.code == "E_DEPLOYMENT"


def test_deploy_returns_result_when_target_was_baked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _image_with_backend(tmp_path)
    image.output_targets("qemu")
    image.bake()

    # Mock the QEMU adapter to not require actual QEMU
    from tdx.deploy import qemu as qemu_mod
    from tdx.models import DeployRequest, DeployResult

    def mock_deploy(self, request: DeployRequest) -> DeployResult:
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

    result = image.deploy(target="qemu", parameters={"region": "local"})
    artifact_path = Path(result.metadata["artifact_path"])

    assert result.target == "qemu"
    assert result.deployment_id == "qemu-default"
    assert artifact_path.exists()
    assert result.metadata["region"] == "local"


def test_deploy_requires_explicit_profile_for_multi_profile_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _image_with_backend(tmp_path)
    with image.profile("dev"):
        image.output_targets("qemu")
    with image.profile("prod"):
        image.output_targets("qemu")
    with image.all_profiles():
        image.bake()

    # Mock QEMU adapter
    from tdx.deploy import qemu as qemu_mod
    from tdx.models import DeployRequest, DeployResult

    def mock_deploy(self, request: DeployRequest) -> DeployResult:
        return DeployResult(
            target="qemu",
            deployment_id=f"qemu-{request.profile}",
            endpoint="ssh://localhost:2222",
            metadata={"artifact_path": str(request.artifact_path)},
        )

    monkeypatch.setattr(qemu_mod.QemuDeployAdapter, "deploy", mock_deploy)

    with image.profiles("dev", "prod"):
        with pytest.raises(ValidationError):
            image.deploy(target="qemu")
        result = image.deploy(target="qemu", profile="dev")

    assert result.deployment_id == "qemu-dev"
