"""Lockfile typed model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tundravm.toolchain import Toolchain

LOCKFILE_VERSION = 6
"""Current lockfile schema version.

Version 2 added per-section digests (``sections``) and version 3 named the
per-profile ones ``variants.<name>.<key>``. Version 4 covers every image-defining
input: the ``distribution`` and ``compiler`` sections, each profile's ``kernel``
and complete ``debloat``, and pins a source build whose source differs between
profiles once per profile (``<profile>/<name>``). Version 5 records the build
``toolchain`` (tundravm, mkosi and its commit, the tools tree's distribution,
release and snapshot, measurement tools; see :class:`~tundravm.toolchain.Toolchain`).
Version 6 names the per-profile sections ``profiles.<name>.<key>``, as the public
API says. Older lockfiles do not load: :func:`~tundravm.lockfile.parse_lockfile`
asks to lock again.
"""


@dataclass(frozen=True, slots=True)
class LockedFetch:
    """A resolved fetch: ``source`` url, ``kind`` (``git``/``http``) and its pin.

    ``digest`` is a commit sha for git and a sha256 for http. Source builds also
    record their ``name`` (``<profile>/<name>`` for a build pinned per profile)
    and the git ``ref`` that was resolved, so a changed ref invalidates the pin.
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
    so drift can be reported section by section. ``toolchain`` is the build
    toolchain recorded at lock time; it is not part of the recipe digest.
    """

    version: int
    recipe_digest: str
    recipe: dict[str, Any]
    dependencies: dict[str, list[str]]
    fetches: list[LockedFetch] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)
    toolchain: Toolchain | None = None
