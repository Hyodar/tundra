"""Lockfile typed model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LOCKFILE_VERSION = 2
"""Current lockfile schema version. Version 2 added per-section digests (``sections``)."""


@dataclass(frozen=True, slots=True)
class LockedFetch:
    source: str
    kind: str
    digest: str


@dataclass(frozen=True, slots=True)
class Lockfile:
    """Parsed ``tundravm.lock``.

    ``recipe_digest`` is the sha256 of the whole canonical recipe payload (what
    frozen bakes enforce). ``sections`` maps dotted payload paths such as
    ``base`` or ``profiles.default.packages`` to the sha256 of that sub-payload,
    so drift can be reported section by section. Version 1 lockfiles have no
    ``sections`` and parse with an empty mapping.
    """

    version: int
    recipe_digest: str
    recipe: dict[str, Any]
    dependencies: dict[str, list[str]]
    fetches: list[LockedFetch] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)
