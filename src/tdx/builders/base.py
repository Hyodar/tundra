"""Typed interfaces for language/toolchain builders."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from tdx.models import Arch


@dataclass(frozen=True, slots=True)
class BuildSpec:
    name: str
    source: Path
    target: Arch
    env: Mapping[str, str] = field(default_factory=dict)


class Builder(Protocol):
    def build(self, spec: BuildSpec) -> Path:
        """Compile source and return output artifact path."""
