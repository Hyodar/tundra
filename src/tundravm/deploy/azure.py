"""Azure deployment adapter.

Uploads a VHD artifact to Azure, publishes it as a confidential-VM-capable
Compute Gallery image version and creates a confidential VM from it.
Requires the `az` CLI to be installed and authenticated.

Secure Boot is off by default: tundravm does not sign the UKI, and Azure's
firmware boots only signed ones with Secure Boot on. ``secure_boot=true``
requires ``signed=true``, the statement that the UKI was signed with keys the
firmware trusts; without it the deploy fails before anything is uploaded.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass

from tundravm.errors import DeploymentError
from tundravm.models import DeployRequest, DeployResult

from ._run import CommandRunner, run_captured, stderr_of

CONTAINER = "tdx-images"
# TDX confidential VMs: the DCesv5/DCedsv5 (and ECesv5/ECedsv5) families.
DEFAULT_VM_SIZE = "Standard_DC2es_v5"
DEFAULT_GALLERY = "tdx_images"


@dataclass(slots=True)
class AzureDeployAdapter:
    name: str = "azure"
    # None: require `az` on PATH and run it via subprocess.
    runner: CommandRunner | None = None

    def deploy(self, request: DeployRequest) -> DeployResult:
        deployment_id = f"azure-{request.profile}-{uuid.uuid4().hex[:8]}"
        params = dict(request.parameters)

        resource_group = params.pop("resource_group", "tdx-vms")
        location = params.pop("location", "eastus")
        vm_size = params.pop("vm_size", DEFAULT_VM_SIZE)
        storage_account = params.pop("storage_account", "")
        gallery = params.pop("gallery", DEFAULT_GALLERY)
        secure_boot = params.pop("secure_boot", "false").lower() == "true"
        signed = params.pop("signed", "false").lower() == "true"
        if secure_boot and not signed:
            raise DeploymentError(
                "Secure Boot needs a signed image; this one is not declared signed.",
                hint=(
                    "Pass --param secure_boot=false (Azure(secure_boot=False)) to boot the "
                    "unsigned UKI, or sign it with keys Azure's firmware trusts and pass "
                    "--param signed=true (Azure(signed=True))."
                ),
                context={"adapter": self.name, "variant": request.profile},
            )

        # Check if az CLI is available
        if self.runner is None and shutil.which("az") is None:
            raise DeploymentError(
                "Azure CLI (`az`) not found in PATH.",
                hint="Install Azure CLI and run `az login` before deploying.",
                context={"adapter": self.name},
            )

        if not request.artifact_path.exists():
            raise DeploymentError(
                "Artifact path does not exist.",
                hint="Run bake() successfully before deploy().",
                context={"artifact_path": str(request.artifact_path)},
            )
        if not storage_account:
            raise DeploymentError(
                "Azure deployment requires a storage_account for VHD upload.",
                hint="Pass storage_account=... in deploy parameters.",
                context={"adapter": self.name},
            )

        blob_name = f"{request.artifact_path.stem}-{uuid.uuid4().hex[:8]}.vhd"
        blob_url = f"https://{storage_account}.blob.core.windows.net/{CONTAINER}/{blob_name}"
        self._az(
            [
                "storage",
                "container",
                "create",
                "--account-name",
                storage_account,
                "--name",
                CONTAINER,
                "--output",
                "json",
            ],
            "Azure storage container creation failed.",
            "Check the storage account name and your permissions on it.",
        )
        self._az(
            [
                "storage",
                "blob",
                "upload",
                "--account-name",
                storage_account,
                "--container-name",
                CONTAINER,
                "--name",
                blob_name,
                "--file",
                str(request.artifact_path),
                "--type",
                "page",
                "--output",
                "json",
            ],
            "Azure VHD upload failed.",
            "Check storage account permissions and connectivity.",
        )
        account_id = self._az(
            ["storage", "account", "show", "--name", storage_account, "--query", "id"],
            "Azure storage account lookup failed.",
            "Check that the storage account exists in this subscription.",
        )

        # A confidential VM boots only a gallery image whose definition supports it.
        definition = f"tdx-{request.profile}"
        version = f"1.0.{int(time.time())}"
        self._az(
            [
                "sig",
                "create",
                "--resource-group",
                resource_group,
                "--gallery-name",
                gallery,
                "--location",
                location,
                "--output",
                "json",
            ],
            "Azure Compute Gallery creation failed.",
            "Check the resource group exists and you may create galleries in it.",
        )
        self._az(
            [
                "sig",
                "image-definition",
                "create",
                "--resource-group",
                resource_group,
                "--gallery-name",
                gallery,
                "--gallery-image-definition",
                definition,
                "--location",
                location,
                "--publisher",
                "tundravm",
                "--offer",
                request.profile,
                "--sku",
                request.profile,
                "--os-type",
                "Linux",
                "--os-state",
                "specialized",
                "--hyper-v-generation",
                "V2",
                "--features",
                "SecurityType=ConfidentialVMSupported",
                "--output",
                "json",
            ],
            "Azure gallery image definition creation failed.",
            "An existing definition of that name must be a specialized V2 Linux one "
            "with SecurityType=ConfidentialVMSupported.",
        )
        image_id = self._az(
            [
                "sig",
                "image-version",
                "create",
                "--resource-group",
                resource_group,
                "--gallery-name",
                gallery,
                "--gallery-image-definition",
                definition,
                "--gallery-image-version",
                version,
                "--location",
                location,
                "--os-vhd-storage-account",
                account_id,
                "--os-vhd-uri",
                blob_url,
                "--query",
                "id",
            ],
            "Azure gallery image version creation failed.",
            "Check the uploaded blob is a fixed-size VHD and the gallery is in its region.",
        )

        # Create VM from the gallery image version
        vm_name = f"tdx-{request.profile}-{uuid.uuid4().hex[:6]}"
        self._az(
            [
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
                image_id,
                "--specialized",
                "--security-type",
                "ConfidentialVM",
                "--os-disk-security-encryption-type",
                "VMGuestStateOnly",
                "--enable-vtpm",
                "true",
                "--enable-secure-boot",
                str(secure_boot).lower(),
                "--public-ip-sku",
                "Standard",
                "--output",
                "json",
            ],
            "Azure VM creation failed.",
            "Check Azure CLI authentication and resource group permissions; "
            f"vm_size must be a TDX confidential size such as {DEFAULT_VM_SIZE}.",
        )

        metadata = {
            "artifact_path": str(request.artifact_path),
            "resource_group": resource_group,
            "location": location,
            "vm_size": vm_size,
            "vm_name": vm_name,
            "blob": f"{CONTAINER}/{blob_name}",
            "image": image_id,
            **params,
        }

        return DeployResult(
            target="azure",
            deployment_id=deployment_id,
            endpoint=f"azure://{resource_group}/{vm_name}",
            metadata=metadata,
        )

    def _az(self, args: list[str], message: str, hint: str) -> str:
        """Run ``az *args``; the value a ``--query`` printed, else its stdout."""
        cmd = ["az", *args]
        if "--query" in args:
            cmd.extend(["--output", "json"])
        runner = self.runner if self.runner is not None else run_captured
        result = runner(cmd)
        if result.returncode != 0:
            raise DeploymentError(
                message,
                hint=hint,
                context={
                    "returncode": str(result.returncode),
                    "stderr": stderr_of(result),
                    "command": " ".join(cmd),
                },
            )
        text = (result.stdout or "").strip()
        if "--query" not in args:
            return text
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        if not isinstance(value, str) or not value:
            raise DeploymentError(
                f"{message.removesuffix('.')}: `az` printed no resource id.",
                hint="Run the command below by hand to see what `az` returns.",
                context={"command": " ".join(cmd), "stdout": text[:2000]},
            )
        return value
