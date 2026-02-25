"""GCP deployment adapter.

Uploads a raw disk image to GCS and creates a Compute Engine VM.
Requires the `gcloud` CLI to be installed and authenticated.
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
class GcpDeployAdapter:
    name: str = "gcp"

    def deploy(self, request: DeployRequest) -> DeployResult:
        deployment_id = f"gcp-{request.profile}-{uuid.uuid4().hex[:8]}"
        params = dict(request.parameters)

        project = params.pop("project", "")
        zone = params.pop("zone", "us-central1-a")
        machine_type = params.pop("machine_type", "n2d-standard-2")
        bucket = params.pop("bucket", "")

        # Check if gcloud is available
        if shutil.which("gcloud") is None:
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

        # Upload image to GCS
        image_name = f"tdx-{request.profile}-{uuid.uuid4().hex[:8]}"
        if bucket:
            gcs_uri = self._upload_image(request.artifact_path, bucket=bucket)
            self._create_image(image_name, gcs_uri=gcs_uri, project=project)
        else:
            gcs_uri = str(request.artifact_path)

        # Create VM instance
        vm_name = f"tdx-{request.profile}-{uuid.uuid4().hex[:6]}"
        cmd = [
            "gcloud",
            "compute",
            "instances",
            "create",
            vm_name,
            "--project",
            project,
            "--zone",
            zone,
            "--machine-type",
            machine_type,
            "--image",
            image_name,
            "--confidential-compute",
            "--maintenance-policy",
            "TERMINATE",
            "--format",
            "json",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise DeploymentError(
                "GCP VM creation failed.",
                hint="Check gcloud authentication and project permissions.",
                context={
                    "returncode": str(result.returncode),
                    "stderr": result.stderr[:2000] if result.stderr else "",
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
            **params,
        }

        return DeployResult(
            target="gcp",
            deployment_id=deployment_id,
            endpoint=f"gcp://{project}/{zone}/{vm_name}",
            metadata=metadata,
        )

    def _upload_image(self, artifact_path: Path, *, bucket: str) -> str:
        blob_name = f"tdx-images/{artifact_path.name}"
        gcs_uri = f"gs://{bucket}/{blob_name}"

        cmd = ["gsutil", "cp", str(artifact_path), gcs_uri]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise DeploymentError(
                "GCS upload failed.",
                hint="Check GCS bucket permissions.",
                context={"bucket": bucket, "stderr": result.stderr[:2000] if result.stderr else ""},
            )
        return gcs_uri

    def _create_image(self, image_name: str, *, gcs_uri: str, project: str) -> None:
        cmd = [
            "gcloud",
            "compute",
            "images",
            "create",
            image_name,
            "--project",
            project,
            "--source-uri",
            gcs_uri,
            "--guest-os-features",
            "UEFI_COMPATIBLE,SEV_SNP_CAPABLE",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise DeploymentError(
                "GCP image creation failed.",
                hint="Check project permissions and image name uniqueness.",
                context={
                    "image_name": image_name,
                    "stderr": result.stderr[:2000] if result.stderr else "",
                },
            )
