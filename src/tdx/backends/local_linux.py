"""Native Linux build execution via mkosi.

Invokes mkosi directly on the host.  By default uses ``sudo`` for privilege
escalation (mkosi needs root or user-namespace support).  Set
``privilege="unshare"`` to use rootless ``unshare --map-auto`` instead, or
``privilege="none"`` to run mkosi as the current user (only works as root).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tdx.backends.base import MountSpec
from tdx.errors import BackendExecutionError
from tdx.models import ArtifactRef, BakeRequest, BakeResult, OutputTarget, ProfileBuildResult


@dataclass(slots=True)
class LocalLinuxBackend:
    name: str = "local_linux"
    privilege: Literal["sudo", "unshare", "none"] = "sudo"
    mkosi_args: list[str] = field(default_factory=list)

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        return (
            MountSpec(source=request.build_dir, target=str(request.build_dir)),
            MountSpec(source=request.emit_dir, target=str(request.emit_dir)),
        )

    def prepare(self, request: BakeRequest) -> None:
        self._ensure_local_prerequisites()
        for mount in self.mount_plan(request):
            mount.source.mkdir(parents=True, exist_ok=True)

    def execute(self, request: BakeRequest) -> BakeResult:
        self._ensure_local_prerequisites()

        # Determine the mkosi project directory for this profile
        mkosi_dir = request.emit_dir / request.profile
        if not mkosi_dir.exists():
            # Fall back to emit_dir itself if no profile subdirectory
            mkosi_dir = request.emit_dir

        output_dir = request.build_dir / request.profile / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build the mkosi command with privilege escalation
        cmd: list[str] = []
        if self.privilege == "unshare" and shutil.which("unshare"):
            cmd.extend(["unshare", "--map-auto", "--map-current-user"])
        elif self.privilege == "sudo" and os.getuid() != 0:
            cmd.append("sudo")

        cmd.extend([
            "mkosi",
            "--force",
            f"--image-id={request.profile}",
            f"--output-dir={output_dir}",
            *self.mkosi_args,
            "build",
        ])

        result = subprocess.run(
            cmd,
            cwd=str(mkosi_dir),
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise BackendExecutionError(
                "mkosi build failed.",
                hint="Check mkosi output for details.",
                context={
                    "backend": self.name,
                    "operation": "execute",
                    "profile": request.profile,
                    "returncode": str(result.returncode),
                    "stderr": result.stderr[:2000] if result.stderr else "",
                    "command": " ".join(cmd),
                },
            )

        # Collect output artifacts
        profile_result = ProfileBuildResult(profile=request.profile)
        artifacts = self._collect_artifacts(output_dir, request)
        profile_result.artifacts = artifacts

        return BakeResult(profiles={request.profile: profile_result})

    def cleanup(self, request: BakeRequest) -> None:
        pass

    def _collect_artifacts(
        self, output_dir: Path, request: BakeRequest
    ) -> dict[OutputTarget, ArtifactRef]:
        """Find and catalog output artifacts from mkosi build."""
        artifacts: dict[OutputTarget, ArtifactRef] = {}

        # UKI format: look for .efi file (may be compressed)
        for efi in sorted(output_dir.glob("*.efi*")):
            artifacts["qemu"] = ArtifactRef(target="qemu", path=efi)
            break

        # Disk image formats (may be compressed with .zst, .xz, etc.)
        for raw in sorted(output_dir.glob("*.raw*")):
            if "qemu" not in artifacts:
                artifacts["qemu"] = ArtifactRef(target="qemu", path=raw)
            break

        for qcow2 in sorted(output_dir.glob("*.qcow2*")):
            artifacts["qemu"] = ArtifactRef(target="qemu", path=qcow2)
            break

        # For Azure/GCP, look for converted formats
        for vhd in sorted(output_dir.glob("*.vhd*")):
            artifacts["azure"] = ArtifactRef(target="azure", path=vhd)
            break

        for tar_gz in sorted(output_dir.glob("*.tar.gz*")):
            artifacts["gcp"] = ArtifactRef(target="gcp", path=tar_gz)
            break

        return artifacts

    def _ensure_local_prerequisites(self) -> None:
        if not sys.platform.startswith("linux"):
            raise BackendExecutionError(
                "Local Linux backend requires a Linux host.",
                hint="Use the Lima backend on non-Linux systems.",
                context={"backend": self.name, "operation": "prepare"},
            )
        if shutil.which("mkosi") is None:
            raise BackendExecutionError(
                "Local Linux backend requires `mkosi` in PATH.",
                hint="Install mkosi and ensure it is available before running bake.",
                context={"backend": self.name, "operation": "prepare"},
            )
