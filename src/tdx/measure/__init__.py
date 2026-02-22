"""Measurement model and backend protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Measurements:
    backend: str
    values: Mapping[str, str] = field(default_factory=dict)


class MeasurementBackend(Protocol):
    name: str

    def derive(self) -> Measurements:
        """Derive measurements for a built image."""


__all__ = ["MeasurementBackend", "Measurements"]
