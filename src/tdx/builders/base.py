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
    output_dir: Path
    install_to: Path | None = None
    reproducible: bool = True
    flags: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BuildArtifact:
    builder: str
    target: Arch
    output_path: Path
    installed_path: Path | None = None
    metadata_path: Path | None = None


class Builder(Protocol):
    def build(self, spec: BuildSpec) -> BuildArtifact:
        """Compile source and return output artifact path."""
