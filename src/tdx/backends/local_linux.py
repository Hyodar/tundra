"""Native Linux build execution."""

from __future__ import annotations

from dataclasses import dataclass

from tdx.backends.base import MountSpec
from tdx.models import BakeRequest, BakeResult


@dataclass(slots=True)
class LocalLinuxBackend:
    name: str = "local_linux"

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        return (
            MountSpec(source=request.build_dir, target=str(request.build_dir)),
            MountSpec(source=request.emit_dir, target=str(request.emit_dir)),
        )

    def prepare(self, request: BakeRequest) -> None:
        _ = request

    def execute(self, request: BakeRequest) -> BakeResult:
        raise NotImplementedError(
            "Local Linux backend execution is not implemented yet.",
        )

    def cleanup(self, request: BakeRequest) -> None:
        _ = request
