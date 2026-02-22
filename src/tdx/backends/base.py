"""Protocol for bake execution backends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tdx.models import BakeRequest, BakeResult


@dataclass(frozen=True, slots=True)
class MountSpec:
    source: Path
    target: str
    read_only: bool = False


class BuildBackend(Protocol):
    name: str

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        """Return deterministic host/guest mount mapping for this request."""

    def prepare(self, request: BakeRequest) -> None:
        """Prepare backend runtime resources."""

    def execute(self, request: BakeRequest) -> BakeResult:
        """Run bake request and return artifacts."""

    def cleanup(self, request: BakeRequest) -> None:
        """Release backend runtime resources."""
