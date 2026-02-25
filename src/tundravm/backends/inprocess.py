"""In-process build backend for testing and development.

Produces deterministic artifacts without invoking mkosi or any external tools.
This backend generates the same mkosi tree as the real backends but creates
placeholder artifacts directly, making it suitable for:
- Unit tests that verify the SDK pipeline
- Development environments without mkosi installed
- CI environments without Linux containers
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from tundravm.backends.base import MountSpec
from tundravm.models import ArtifactRef, BakeRequest, BakeResult, ProfileBuildResult

# Map output targets to artifact filenames
TARGET_FILENAMES: dict[str, str] = {
    "qemu": "disk.qcow2",
    "azure": "disk.vhd",
    "gcp": "disk.raw.tar.gz",
}


@dataclass(slots=True)
class InProcessBackend:
    """Backend that produces deterministic placeholder artifacts in-process."""

    name: str = "inprocess"

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        return (
            MountSpec(source=request.build_dir, target=str(request.build_dir)),
            MountSpec(source=request.emit_dir, target=str(request.emit_dir)),
        )

    def prepare(self, request: BakeRequest) -> None:
        for mount in self.mount_plan(request):
            mount.source.mkdir(parents=True, exist_ok=True)

    def execute(self, request: BakeRequest) -> BakeResult:
        profile_dir = request.build_dir / request.profile
        profile_dir.mkdir(parents=True, exist_ok=True)

        profile_result = ProfileBuildResult(profile=request.profile)

        for target in request.output_targets:
            filename = TARGET_FILENAMES.get(target, f"{target}.img")
            artifact_path = profile_dir / filename
            content = (
                f"tdx-artifact: profile={request.profile} target={target}\n"
                f"digest={hashlib.sha256(f'{request.profile}:{target}'.encode()).hexdigest()}\n"
            )
            artifact_path.write_text(content, encoding="utf-8")
            profile_result.artifacts[target] = ArtifactRef(
                target=target,
                path=artifact_path,
            )

        return BakeResult(profiles={request.profile: profile_result})

    def cleanup(self, request: BakeRequest) -> None:
        pass
