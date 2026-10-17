"""In-process build backend for testing and development.

Produces deterministic artifacts without invoking mkosi or any external tools.
This backend generates the same mkosi tree as the real backends but creates
placeholder artifacts directly (each disk records the simulated UKI ``disk.efi`` beside
it as its boot payload), making it suitable for:
- Unit tests that verify the SDK pipeline
- Development environments without mkosi installed
- CI environments without Linux containers
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from tundravm.backends.base import MountSpec, Requirement
from tundravm.models import ArtifactRef, BakeRequest, BakeResult, ProfileBuildResult

# Map output targets to artifact filenames
TARGET_FILENAMES: dict[str, str] = {
    "qemu": "disk.qcow2",
    "azure": "disk.vhd",
    "gcp": "disk.raw.tar.gz",
}
UKI_FILENAME = "disk.efi"
"""The simulated UKI every disk artifact boots: one per variant, so the disks share it."""


@dataclass(slots=True)
class InProcessBackend:
    """Backend that produces deterministic placeholder artifacts in-process."""

    name: str = "inprocess"

    def requirements(self) -> tuple[Requirement, ...]:
        return ()

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        return (
            MountSpec(source=request.build_dir, target=str(request.build_dir)),
            MountSpec(source=request.emit_dir, target=str(request.emit_dir)),
        )

    def prepare(self, request: BakeRequest) -> None:
        for mount in self.mount_plan(request):
            mount.source.mkdir(parents=True, exist_ok=True)

    def execute(self, request: BakeRequest) -> BakeResult:
        emit = request.on_output if request.on_output is not None else _discard
        profile_dir = request.build_dir / request.profile
        profile_dir.mkdir(parents=True, exist_ok=True)
        emit(f"inprocess: building variant {request.profile} from {request.emit_dir}")

        profile_result = ProfileBuildResult(profile=request.profile)
        uki = profile_dir / UKI_FILENAME
        uki.write_text(f"tdx-uki: profile={request.profile}\n", encoding="utf-8")

        for target in request.output_targets:
            artifact_path = profile_dir / TARGET_FILENAMES.get(target, f"{target}.img")
            content = (
                f"tdx-artifact: profile={request.profile} target={target}\n"
                f"digest={hashlib.sha256(f'{request.profile}:{target}'.encode()).hexdigest()}\n"
            )
            artifact_path.write_text(content, encoding="utf-8")
            profile_result.artifacts[target] = ArtifactRef(
                target=target,
                path=artifact_path,
                boot_payload=None if artifact_path.suffix == ".efi" else uki,
            )
            emit(f"inprocess: wrote {target} artifact {artifact_path.name}")

        emit(f"inprocess: done ({len(profile_result.artifacts)} artifacts)")
        return BakeResult(profiles={request.profile: profile_result})

    def cleanup(self, request: BakeRequest) -> None:
        pass

    def fingerprint(self) -> dict[str, object]:
        """``{"name": "inprocess"}``: it has no settings."""
        return {"name": self.name}


def _discard(line: str) -> None:
    del line
