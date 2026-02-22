"""mkosi emission contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from tdx.ir.model import ImageIR


@dataclass(frozen=True, slots=True)
class MkosiEmission:
    root: Path
    profile_paths: dict[str, Path] = field(default_factory=dict)


class MkosiEmitter(Protocol):
    def emit(self, ir: ImageIR, destination: Path) -> MkosiEmission:
        """Emit mkosi tree and return metadata about generated files."""
