"""Script emission contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ScriptEmission:
    profile: str
    scripts: dict[str, Path] = field(default_factory=dict)
