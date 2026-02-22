"""Lima-backed build execution."""

from __future__ import annotations

from dataclasses import dataclass

from tdx.models import BakeRequest, BakeResult


@dataclass(slots=True)
class LimaBackend:
    name: str = "lima"

    def prepare(self, request: BakeRequest) -> None:
        _ = request

    def execute(self, request: BakeRequest) -> BakeResult:
        raise NotImplementedError(
            "Lima backend execution is not implemented yet.",
        )

    def cleanup(self, request: BakeRequest) -> None:
        _ = request
