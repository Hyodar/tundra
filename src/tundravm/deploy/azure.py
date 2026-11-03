"""Azure deployment adapter.

Uploads a VHD artifact to Azure, publishes it as a confidential-VM-capable
Compute Gallery image version and creates a confidential VM from it.
Requires the `az` CLI to be installed and authenticated.

Each volume's disk is attached at the LUN its image recorded (``NAME@LUN=create:SIZE`` or
``NAME@LUN=existing:ID`` in ``volumes``): a ``create`` disk is made first with ``az disk
create``, then ``az vm create --attach-data-disks`` takes every disk in LUN order, which
azure-cli attaches at LUN 0, 1, ... (``normalize_disk_info``, azure-cli ``18663606``), so
the disks are there at first boot. Each is detached, never deleted, with the VM.

The VM gets no network security group rule (``--nsg-rule NONE``): the image's firewall
decides what is open, and Azure's default SSH rule would contradict it. The receipt records
the public address ``az vm create`` prints.

Secure Boot is off by default: tundravm does not sign the UKI, and Azure's
firmware boots only signed ones with Secure Boot on. ``secure_boot=true``
requires ``signed=true``, the statement that the UKI was signed with keys the
firmware trusts; without it the deploy fails before anything is uploaded.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass

from tundravm.errors import DeploymentError
from tundravm.models import DeployRequest, DeployResult

from ._run import RANDOM, CommandRunner, Fresh, run_captured, stderr_of

CONTAINER = "tdx-images"
# TDX confidential VMs: the DCesv5/DCedsv5 (and ECesv5/ECedsv5) families.
DEFAULT_VM_SIZE = "Standard_DC2es_v5"
DEFAULT_GALLERY = "tdx_images"
CONFIDENTIAL_FLAGS = (
    "--security-type",
    "--os-disk-security-encryption-type",
    "--enable-vtpm",
    "--enable-secure-boot",
)
"""The ``vm create`` flags that make the VM a confidential one."""


@dataclass(slots=True)
class AzureDeployAdapter:
    name: str = "azure"
    # None: require `az` on PATH and run it via subprocess.
    runner: CommandRunner | None = None
    fresh: Fresh = RANDOM

    def deploy(self, request: DeployRequest) -> DeployResult:
        deployment_id = f"azure-{request.profile}-{self.fresh.hex(8)}"
        params = dict(request.parameters)

        resource_group = params.pop("resource_group", "tdx-vms")
        location = params.pop("location", "eastus")
        vm_size = params.pop("vm_size", DEFAULT_VM_SIZE)
        storage_account = params.pop("storage_account", "")
        gallery = params.pop("gallery", DEFAULT_GALLERY)
        secure_boot = params.pop("secure_boot", "false").lower() == "true"
        signed = params.pop("signed", "false").lower() == "true"
        volumes = attachments(params.pop("volumes", ""))
        if secure_boot and not signed:
            raise DeploymentError(
                "Secure Boot needs a signed image; this one is not declared signed.",
                hint=(
                    "Pass --param secure_boot=false (Azure(secure_boot=False)) to boot the "
                    "unsigned UKI, or sign it with keys Azure's firmware trusts and pass "
                    "--param signed=true (Azure(signed=True))."
                ),
                context={"adapter": self.name, "profile": request.profile},
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
                hint="Run `tundravm build` first, then deploy the build.",
                context={"artifact_path": str(request.artifact_path)},
            )
        if not storage_account:
            raise DeploymentError(
                "Azure deployment requires a storage_account for VHD upload.",
                hint="Pass storage_account=... in deploy parameters.",
                context={"adapter": self.name},
            )

        blob_name = f"{request.artifact_path.stem}-{self.fresh.hex(8)}.vhd"
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
        version = f"1.0.{self.fresh.epoch()}"
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

        disks: dict[str, str] = {}
        for _, name, how, value in volumes:
            if how == "existing":
                disks[name] = value
                continue
            disks[name] = self._az(
                [
                    "disk",
                    "create",
                    "--resource-group",
                    resource_group,
                    "--name",
                    f"{request.profile}-{name}-{self.fresh.hex(6)}",
                    "--location",
                    location,
                    "--size-gb",
                    value,
                    "--query",
                    "id",
                ],
                f"Azure disk creation for volume {name} failed.",
                "Check the resource group and the region's disk quota.",
            )
        attach = (
            ["--attach-data-disks", *disks.values(), "--data-disk-delete-option", "Detach"]
            if disks
            else []
        )

        # Create VM from the gallery image version
        vm_name = f"tdx-{request.profile}-{self.fresh.hex(6)}"
        created = self._az(
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
                "--nsg-rule",
                "NONE",
                *attach,
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
            "public_ip": _public_ip(created),
            **({"disks": disks, "luns": {n: lun for lun, n, _, _ in volumes}} if disks else {}),
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


_SIZE = re.compile(r"([1-9][0-9]*)(G|GB|GiB|T|TB|TiB)?")


def attachments(text: str) -> list[tuple[int, str, str, str]]:
    """``NAME@LUN=create:SIZE;NAME@LUN=existing:ID`` as ``(lun, name, how, value)`` in LUN
    order, a size in whole GiB (Azure's GB): ``100``, ``100GiB``, ``2TiB``. The LUNs are
    0, 1, ...: what ``--attach-data-disks`` gives the disks in that order."""
    found: list[tuple[int, str, str, str]] = []
    for item in filter(None, (part.strip() for part in text.split(";"))):
        head, _, spec = item.partition("=")
        name, _, lun = head.partition("@")
        how, _, value = spec.partition(":")
        if how not in ("create", "existing") or not lun.isdigit() or not value:
            raise DeploymentError(
                f"Invalid volume {item!r}.",
                hint="Write NAME@LUN=create:SIZE or NAME@LUN=existing:ID.",
            )
        if how == "create":
            size = _SIZE.fullmatch(value)
            if size is None:
                raise DeploymentError(
                    f"Volume {name}: {value!r} is not a size Azure takes.",
                    hint="Give whole GiB or TiB: --volume data=create:100GiB.",
                )
            value = str(int(size[1]) * (1024 if (size[2] or "G").startswith("T") else 1))
        found.append((int(lun), name, how, value))
    found.sort()
    luns = [lun for lun, *_ in found]
    if luns != list(range(len(found))):
        raise DeploymentError(
            f"The volumes' LUNs are {luns}, not 0 to {len(found) - 1}.",
            hint="Attach every volume the image declares: each one's LUN is its position.",
        )
    return found


def _public_ip(text: str) -> str:
    """The ``publicIpAddress`` ``az vm create`` prints, empty when it printed none."""
    try:
        value = json.loads(text).get("publicIpAddress")
    except (ValueError, AttributeError):
        return ""
    return value if isinstance(value, str) else ""
