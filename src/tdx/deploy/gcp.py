"""GCP deployment adapter."""

from __future__ import annotations

from dataclasses import dataclass

from tdx.models import DeployRequest, DeployResult


@dataclass(slots=True)
class GcpDeployAdapter:
    name: str = "gcp"

    def deploy(self, request: DeployRequest) -> DeployResult:
        deployment_id = f"gcp-{request.profile}"
        metadata = {"artifact_path": str(request.artifact_path), **dict(request.parameters)}
        return DeployResult(
            target="gcp",
            deployment_id=deployment_id,
            endpoint=f"gcp://images/{deployment_id}",
            metadata=metadata,
        )
