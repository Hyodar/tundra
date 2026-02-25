"""Lockfile typed model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class LockedFetch:
    source: str
    kind: str
    digest: str


@dataclass(frozen=True, slots=True)
class Lockfile:
    version: int
    recipe_digest: str
    recipe: dict[str, Any]
    dependencies: dict[str, list[str]]
    fetches: list[LockedFetch] = field(default_factory=list)
