"""Azure deployment adapter.

Uploads a VHD artifact to Azure and creates a VM from it.
Requires the `az` CLI to be installed and authenticated.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from tundravm.errors import DeploymentError
from tundravm.models import DeployRequest, DeployResult


@dataclass(slots=True)
class AzureDeployAdapter:
    name: str = "azure"

    def deploy(self, request: DeployRequest) -> DeployResult:
        deployment_id = f"azure-{request.profile}-{uuid.uuid4().hex[:8]}"
        params = dict(request.parameters)

        resource_group = params.pop("resource_group", "tdx-vms")
        location = params.pop("location", "eastus")
        vm_size = params.pop("vm_size", "Standard_DC2s_v3")
        storage_account = params.pop("storage_account", "")

        # Check if az CLI is available
        if shutil.which("az") is None:
            raise DeploymentError(
                "Azure CLI (`az`) not found in PATH.",
                hint="Install Azure CLI and run `az login` before deploying.",
                context={"adapter": self.name},
            )

        # Upload VHD to Azure blob storage
        if storage_account:
            blob_url = self._upload_vhd(
                request.artifact_path,
                storage_account=storage_account,
                resource_group=resource_group,
            )
        else:
            blob_url = str(request.artifact_path)

        # Create VM from the uploaded VHD
        vm_name = f"tdx-{request.profile}-{uuid.uuid4().hex[:6]}"
        cmd = [
            "az",
            "vm",
            "create",
            "--resource-group",
            resource_group,
            "--name",
            vm_name,
            "--location",
            location,
            "--size",
            vm_size,
            "--image",
            blob_url,
            "--security-type",
            "ConfidentialVM",
            "--os-disk-security-encryption-type",
            "VMGuestStateOnly",
            "--output",
            "json",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise DeploymentError(
                "Azure VM creation failed.",
                hint="Check Azure CLI authentication and resource group permissions.",
                context={
                    "returncode": str(result.returncode),
                    "stderr": result.stderr[:2000] if result.stderr else "",
                    "command": " ".join(cmd),
                },
            )

        metadata = {
            "artifact_path": str(request.artifact_path),
            "resource_group": resource_group,
            "location": location,
            "vm_size": vm_size,
            "vm_name": vm_name,
            **params,
        }

        return DeployResult(
            target="azure",
            deployment_id=deployment_id,
            endpoint=f"azure://{resource_group}/{vm_name}",
            metadata=metadata,
        )

    def _upload_vhd(self, artifact_path: Path, *, storage_account: str, resource_group: str) -> str:
        """Upload VHD to Azure blob storage."""
        container = "tdx-images"
        blob_name = f"{artifact_path.stem}-{uuid.uuid4().hex[:8]}.vhd"

        cmd = [
            "az",
            "storage",
            "blob",
            "upload",
            "--account-name",
            storage_account,
            "--container-name",
            container,
            "--name",
            blob_name,
            "--file",
            str(artifact_path),
            "--type",
            "page",
            "--output",
            "json",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise DeploymentError(
                "Azure VHD upload failed.",
                hint="Check storage account permissions and connectivity.",
                context={
                    "storage_account": storage_account,
                    "stderr": result.stderr[:2000] if result.stderr else "",
                },
            )

        return f"https://{storage_account}.blob.core.windows.net/{container}/{blob_name}"
