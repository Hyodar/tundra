"""Lockfile typed model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LOCKFILE_VERSION = 2
"""Current lockfile schema version. Version 2 added per-section digests (``sections``)."""


@dataclass(frozen=True, slots=True)
class LockedFetch:
    """A resolved fetch: ``source`` url, ``kind`` (``git``/``http``) and its pin.

    ``digest`` is a commit sha for git and a sha256 for http. Source builds also
    record their ``name`` and the git ``ref`` that was resolved, so a changed ref
    invalidates the pin.
    """

    source: str
    kind: str
    digest: str
    name: str | None = None
    ref: str | None = None


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
