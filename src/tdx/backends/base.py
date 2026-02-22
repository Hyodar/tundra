"""Protocol for bake execution backends."""

from __future__ import annotations

from typing import Protocol

from tdx.models import BakeRequest, BakeResult


class BuildBackend(Protocol):
    name: str

    def prepare(self, request: BakeRequest) -> None:
        """Prepare backend runtime resources."""

    def execute(self, request: BakeRequest) -> BakeResult:
        """Run bake request and return artifacts."""

    def cleanup(self, request: BakeRequest) -> None:
        """Release backend runtime resources."""
