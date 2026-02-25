from pathlib import Path

import pytest

from tundravm.deploy import get_adapter
from tundravm.deploy.azure import AzureDeployAdapter
from tundravm.deploy.gcp import GcpDeployAdapter
from tundravm.deploy.qemu import QemuDeployAdapter
from tundravm.errors import DeploymentError
from tundravm.models import DeployRequest, OutputTarget


def test_qemu_adapter_requires_qemu_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QEMU adapter raises when qemu-system-x86_64 is not found."""
    request = _request(tmp_path, target="qemu")
    monkeypatch.setattr("tundravm.deploy.qemu.shutil.which", lambda _: None)

    with pytest.raises(DeploymentError, match="QEMU binary not found"):
        QemuDeployAdapter().deploy(request)


def test_azure_adapter_requires_az_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Azure adapter raises when az CLI is not found."""
    request = _request(tmp_path, target="azure")
    monkeypatch.setattr("tundravm.deploy.azure.shutil.which", lambda _: None)

    with pytest.raises(DeploymentError, match="Azure CLI"):
        AzureDeployAdapter().deploy(request)


def test_gcp_adapter_requires_gcloud_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GCP adapter raises when gcloud is not found."""
    request = _request(tmp_path, target="gcp")
    monkeypatch.setattr("tundravm.deploy.gcp.shutil.which", lambda _: None)

    with pytest.raises(DeploymentError, match="gcloud"):
        GcpDeployAdapter().deploy(request)


def test_gcp_adapter_requires_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GCP adapter raises when no project is specified."""
    request = _request(tmp_path, target="gcp")
    monkeypatch.setattr("tundravm.deploy.gcp.shutil.which", lambda _: "/usr/bin/gcloud")

    with pytest.raises(DeploymentError, match="project is required"):
        GcpDeployAdapter().deploy(request)


def test_get_adapter_rejects_unsupported_target() -> None:
    with pytest.raises(DeploymentError):
        get_adapter("unsupported")


def test_get_adapter_returns_correct_types() -> None:
    assert isinstance(get_adapter("qemu"), QemuDeployAdapter)
    assert isinstance(get_adapter("azure"), AzureDeployAdapter)
    assert isinstance(get_adapter("gcp"), GcpDeployAdapter)


def _request(tmp_path: Path, *, target: OutputTarget) -> DeployRequest:
    artifact = tmp_path / f"{target}.img"
    artifact.write_text("artifact", encoding="utf-8")
    return DeployRequest(
        profile="default",
        target=target,
        artifact_path=artifact,
        parameters={"region": "local"},
    )
