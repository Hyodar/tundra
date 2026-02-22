"""Lima-backed build execution."""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from tdx.backends.base import MountSpec
from tdx.errors import BackendExecutionError
from tdx.models import BakeRequest, BakeResult, ProfileBuildResult


@dataclass(slots=True)
class LimaBackend:
    """Lima backend with deterministic mount mapping and basic contract wiring."""

    name: str = "lima"
    instance_name: str = "tdx-builder"
    mount_prefix: str = "/mnt/host"

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        """Mount plan is ordered and stable to avoid backend drift across runs."""
        mounts = [
            MountSpec(source=request.build_dir, target=f"{self.mount_prefix}/build"),
            MountSpec(source=request.emit_dir, target=f"{self.mount_prefix}/emit"),
        ]
        # Deterministic order by guest mount target.
        return tuple(sorted(mounts, key=lambda mount: mount.target))

    def prepare(self, request: BakeRequest) -> None:
        self._ensure_lima_available()
        for mount in self.mount_plan(request):
            mount.source.mkdir(parents=True, exist_ok=True)

    def execute(self, request: BakeRequest) -> BakeResult:
        self._ensure_lima_available()
        profile_result = ProfileBuildResult(profile=request.profile)
        return BakeResult(profiles={request.profile: profile_result})

    def cleanup(self, request: BakeRequest) -> None:
        _ = request

    def _ensure_lima_available(self) -> None:
        if shutil.which("limactl") is None:
            raise BackendExecutionError(
                "Lima backend requires `limactl` in PATH.",
                hint="Install Lima and ensure `limactl` is available before running bake.",
                context={"backend": self.name, "operation": "prepare"},
            )
