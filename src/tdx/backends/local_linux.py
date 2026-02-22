"""Native Linux build execution."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

from tdx.backends.base import MountSpec
from tdx.errors import BackendExecutionError
from tdx.models import BakeRequest, BakeResult, ProfileBuildResult


@dataclass(slots=True)
class LocalLinuxBackend:
    name: str = "local_linux"

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
        profile_result = ProfileBuildResult(profile=request.profile)
        return BakeResult(profiles={request.profile: profile_result})

    def cleanup(self, request: BakeRequest) -> None:
        _ = request

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
