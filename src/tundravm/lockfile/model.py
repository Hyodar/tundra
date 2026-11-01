"""Lockfile typed model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tundravm.toolchain import Toolchain

LOCKFILE_VERSION = 8
"""Current lockfile schema version.

Version 2 added per-section digests (``sections``) and version 3 named the
per-profile ones ``variants.<name>.<key>``. Version 4 covers every image-defining
input: the ``distribution`` and ``compiler`` sections, each profile's ``kernel``
and complete ``debloat``, and pins a source build whose source differs between
profiles once per profile (``<profile>/<name>``). Version 5 records the build
``toolchain`` (tundravm, mkosi and its commit, the tools tree's distribution,
release and snapshot, measurement tools; see :class:`~tundravm.toolchain.Toolchain`).
Version 6 names the per-profile sections ``profiles.<name>.<key>``, as the public
API says. Version 7 names the payload and its digest ``image`` and ``image_digest``,
as the build manifest does, and records each digest-pinned apt archive's ``InRelease``
(``repositories``). Version 8 pins each Dotnet build's SDK (``toolchains``): the
release its ``global.json`` resolves to, with the tarball's url and sha512. Older
lockfiles do not load: :func:`~tundravm.lockfile.parse_lockfile` asks to lock again.
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
class LockedRepository:
    """An ``AptRepository(sha256=)`` as ``lock`` found it: its ``InRelease``'s ``sha256``
    and ``Date:``, which an offline build checks instead of fetching the file."""

    name: str
    url: str
    suite: str
    sha256: str
    date: str


@dataclass(frozen=True, slots=True)
class LockedToolchain:
    """A toolchain ``lock`` resolved for a source build, which ``build`` fetches as recorded.

    ``kind`` is ``dotnet-sdk``: *requested* is what the ``global.json`` of ``name``'s
    checkout at the pin *source* asks for (``9.0.2 latestFeature``), *version* the SDK
    release satisfying it, *url* its ``linux-x64`` tarball and *sha512* the tarball's hash,
    both as the .NET release manifest states them. A changed source pin invalidates it.
    """

    name: str
    kind: str
    source: str
    requested: str
    version: str
    url: str
    sha512: str


@dataclass(frozen=True, slots=True)
class Lockfile:
    """Parsed ``tundravm.lock``.

    ``image_digest`` is the sha256 of the whole canonical image payload (what
    frozen builds enforce). ``sections`` maps dotted payload paths such as
    ``base`` or ``profiles.default.packages`` to the sha256 of that sub-payload,
    so drift can be reported section by section. ``toolchain`` is the build
    toolchain recorded at lock time; it is not part of the image digest.
    ``toolchains`` pins what a source build's own toolchain file resolves to
    (:class:`LockedToolchain`).
    """

    version: int
    image_digest: str
    image: dict[str, Any]
    dependencies: dict[str, list[str]]
    fetches: list[LockedFetch] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)
    toolchain: Toolchain | None = None
    repositories: list[LockedRepository] = field(default_factory=list)
    toolchains: list[LockedToolchain] = field(default_factory=list)
