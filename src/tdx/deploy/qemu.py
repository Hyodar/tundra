"""QEMU deployment adapter."""

from __future__ import annotations

from dataclasses import dataclass

from tdx.models import DeployRequest, DeployResult


@dataclass(slots=True)
class QemuDeployAdapter:
    name: str = "qemu"

    def deploy(self, request: DeployRequest) -> DeployResult:
        deployment_id = f"qemu-{request.profile}"
        metadata = {"artifact_path": str(request.artifact_path), **dict(request.parameters)}
        return DeployResult(
            target="qemu",
            deployment_id=deployment_id,
            endpoint=f"qemu://local/{deployment_id}",
            metadata=metadata,
        )
