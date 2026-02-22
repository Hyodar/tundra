from pathlib import Path

import pytest

from tdx.deploy import AzureDeployAdapter, GcpDeployAdapter, QemuDeployAdapter, get_adapter
from tdx.errors import DeploymentError
from tdx.models import DeployRequest, OutputTarget


def test_qemu_adapter_returns_typed_result(tmp_path: Path) -> None:
    request = _request(tmp_path, target="qemu")
    result = QemuDeployAdapter().deploy(request)

    assert result.target == "qemu"
    assert result.deployment_id == "qemu-default"
    assert result.endpoint == "qemu://local/qemu-default"
    assert result.metadata["artifact_path"] == str(request.artifact_path)


def test_azure_adapter_returns_typed_result(tmp_path: Path) -> None:
    request = _request(tmp_path, target="azure")
    result = AzureDeployAdapter().deploy(request)

    assert result.target == "azure"
    assert result.deployment_id == "azure-default"
    assert result.endpoint == "azure://images/azure-default"
    assert result.metadata["artifact_path"] == str(request.artifact_path)


def test_gcp_adapter_returns_typed_result(tmp_path: Path) -> None:
    request = _request(tmp_path, target="gcp")
    result = GcpDeployAdapter().deploy(request)

    assert result.target == "gcp"
    assert result.deployment_id == "gcp-default"
    assert result.endpoint == "gcp://images/gcp-default"
    assert result.metadata["artifact_path"] == str(request.artifact_path)


def test_get_adapter_rejects_unsupported_target() -> None:
    with pytest.raises(DeploymentError):
        get_adapter("unsupported")


def _request(tmp_path: Path, *, target: OutputTarget) -> DeployRequest:
    artifact = tmp_path / f"{target}.img"
    artifact.write_text("artifact", encoding="utf-8")
    return DeployRequest(
        profile="default",
        target=target,
        artifact_path=artifact,
        parameters={"region": "local"},
    )
