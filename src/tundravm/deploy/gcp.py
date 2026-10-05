"""GCP deployment adapter.

Uploads a raw disk image to GCS, creates a TDX-capable Compute Engine image and
an Intel TDX Confidential VM from it.
Requires the `gcloud` CLI to be installed and authenticated, and current: one
whose GA ``gcloud compute instances create`` takes ``--confidential-compute-type=TDX``
(any release since Intel TDX on C3 became generally available, late 2024). Such a
gcloud has ``gcloud storage cp``, which the upload calls directly; ``gsutil`` is
not used. ``gcloud components update`` brings an older one up to date.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from tundravm.errors import DeploymentError
from tundravm.models import DeployRequest, DeployResult

from ._run import CommandRunner, run_captured, stderr_of

# Intel TDX Confidential VMs run on the C3 machine series.
DEFAULT_MACHINE_TYPE = "c3-standard-4"
GUEST_OS_FEATURES = "UEFI_COMPATIBLE,GVNIC,TDX_CAPABLE"


@dataclass(slots=True)
class GcpDeployAdapter:
    name: str = "gcp"
    # None: require `gcloud` on PATH and run it via subprocess.
    runner: CommandRunner | None = None

    def deploy(self, request: DeployRequest) -> DeployResult:
        deployment_id = f"gcp-{request.profile}-{uuid.uuid4().hex[:8]}"
        params = dict(request.parameters)

        project = params.pop("project", "")
        zone = params.pop("zone", "us-central1-a")
        machine_type = params.pop("machine_type", DEFAULT_MACHINE_TYPE)
        bucket = params.pop("bucket", "")

        # Check if gcloud is available
        if self.runner is None and shutil.which("gcloud") is None:
            raise DeploymentError(
                "Google Cloud CLI (`gcloud`) not found in PATH.",
                hint="Install gcloud CLI and run `gcloud auth login` before deploying.",
                context={"adapter": self.name},
            )

        if not project:
            raise DeploymentError(
                "GCP project is required for deployment.",
                hint="Pass project= in deploy parameters.",
                context={"adapter": self.name},
            )
        if not bucket:
            raise DeploymentError(
                "GCP deployment requires a GCS bucket for image upload.",
                hint="Pass bucket=... in deploy parameters.",
                context={"adapter": self.name},
            )
        if not request.artifact_path.exists():
            raise DeploymentError(
                "Artifact path does not exist.",
                hint="Run bake() successfully before deploy().",
                context={"artifact_path": str(request.artifact_path)},
            )

        # Upload image to GCS
        image_name = f"tdx-{request.profile}-{uuid.uuid4().hex[:8]}"
        gcs_uri = self._upload_image(request.artifact_path, bucket=bucket, image_name=image_name)
        self._create_image(image_name, gcs_uri=gcs_uri, project=project)

        # Create VM instance
        vm_name = f"tdx-{request.profile}-{uuid.uuid4().hex[:6]}"
        cmd = [
            "gcloud",
            "compute",
            "instances",
            "create",
            vm_name,
            f"--project={project}",
            f"--zone={zone}",
            f"--machine-type={machine_type}",
            f"--image={image_name}",
            "--confidential-compute-type=TDX",
            "--maintenance-policy=TERMINATE",
            "--format=json",
        ]
        result = self._run(cmd)
        if result.returncode != 0:
            raise DeploymentError(
                "GCP VM creation failed.",
                hint=(
                    "Check gcloud authentication and project permissions; machine_type "
                    f"must be a C3 type such as {DEFAULT_MACHINE_TYPE} in a zone offering TDX."
                ),
                context={
                    "returncode": str(result.returncode),
                    "stderr": stderr_of(result),
                    "command": " ".join(cmd),
                },
            )

        metadata = {
            "artifact_path": str(request.artifact_path),
            "project": project,
            "zone": zone,
            "machine_type": machine_type,
            "vm_name": vm_name,
            "image_name": image_name,
            "blob": gcs_uri,
            **params,
        }

        return DeployResult(
            target="gcp",
            deployment_id=deployment_id,
            endpoint=f"gcp://{project}/{zone}/{vm_name}",
            metadata=metadata,
        )

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        runner = self.runner if self.runner is not None else run_captured
        return runner(cmd)

    def _upload_image(self, artifact_path: Path, *, bucket: str, image_name: str) -> str:
        gcs_uri = f"gs://{bucket}/tdx-images/{image_name}.tar.gz"
        cmd = ["gcloud", "storage", "cp", str(artifact_path), gcs_uri]
        result = self._run(cmd)
        if result.returncode != 0:
            raise DeploymentError(
                "GCS upload failed.",
                hint=(
                    "Check GCS bucket permissions; if `gcloud storage` is unknown, update "
                    "the gcloud CLI (`gcloud components update`)."
                ),
                context={"bucket": bucket, "stderr": stderr_of(result), "command": " ".join(cmd)},
            )
        return gcs_uri

    def _create_image(self, image_name: str, *, gcs_uri: str, project: str) -> None:
        cmd = [
            "gcloud",
            "compute",
            "images",
            "create",
            image_name,
            f"--project={project}",
            f"--source-uri={gcs_uri}",
            f"--guest-os-features={GUEST_OS_FEATURES}",
        ]
        result = self._run(cmd)
        if result.returncode != 0:
            raise DeploymentError(
                "GCP image creation failed.",
                hint="Check project permissions and image name uniqueness.",
                context={
                    "image_name": image_name,
                    "stderr": stderr_of(result),
                    "command": " ".join(cmd),
                },
            )
