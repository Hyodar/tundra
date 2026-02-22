"""Azure deployment adapter."""

from __future__ import annotations

from dataclasses import dataclass

from tdx.models import DeployRequest, DeployResult


@dataclass(slots=True)
class AzureDeployAdapter:
    name: str = "azure"

    def deploy(self, request: DeployRequest) -> DeployResult:
        deployment_id = f"azure-{request.profile}"
        metadata = {
            "artifact_path": str(request.artifact_path),
            "implementation_mode": "simulated",
            **dict(request.parameters),
        }
        return DeployResult(
            target="azure",
            deployment_id=deployment_id,
            endpoint=f"azure://images/{deployment_id}",
            metadata=metadata,
        )
