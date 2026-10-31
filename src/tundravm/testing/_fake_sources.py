"""A stand-in for git and http at the fetch boundary of :mod:`tundravm._source`.

:class:`FakeSources` replaces ``fetch_source``, the git queries that verify a checkout and
the host-toolchain dependency prefetch, so a build runs its whole fetch step (pins, the
``.sources`` layout, the completion markers, the reuse of a complete checkout) while nothing
reaches the network and no ``git``, ``go`` or ``cargo`` runs: each checkout is a directory
holding one file that names the source and the pin.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tundravm._source import Source, SourceBuild

CHECKOUT_FILE = "FAKE-CHECKOUT"
"""The one file a fake checkout holds: the source's url and the pin, as JSON."""


@dataclass(frozen=True, slots=True)
class FakeFetch:
    """One recorded checkout: the source's url, the pin and where it landed."""

    url: str
    pin: str
    path: Path


@dataclass(slots=True)
class FakeSources:
    """Records source checkouts instead of fetching them.

    ``fetch_source`` writes :data:`CHECKOUT_FILE` into the new checkout; a fake checkout
    answers git's ``rev-parse`` with its pin and ``status`` with a clean tree, so a second
    build reuses it. Dependency prefetches are skipped (``None``: the build would download
    them). *fetched* lists every checkout made, in order.
    """

    fetched: list[FakeFetch] = field(default_factory=list)

    def fetch_source(self, source: Source, pin: str, dest: Path) -> None:
        dest.mkdir(parents=True)
        text = json.dumps({"url": source.url, "pin": pin}, sort_keys=True) + "\n"
        (dest / CHECKOUT_FILE).write_text(text, encoding="utf-8")
        self.fetched.append(FakeFetch(source.url, pin, dest))

    def git_output(self, checkout: Path, *args: str) -> str | None:
        try:
            recorded = json.loads((Path(checkout) / CHECKOUT_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if args[:1] == ("rev-parse",):
            return f"{recorded['pin']}\n"
        return ""

    def prefetch_deps(self, build: SourceBuild, pin: str, *args: object, **kwargs: object) -> None:
        del build, pin, args, kwargs

    def install(self, setattr: Callable[[object, str, object], None]) -> FakeSources:
        """Put this fake in place of the fetch boundary using *setattr* (pytest's
        ``monkeypatch.setattr``, which undoes it after the test)."""
        from tundravm import _source
        from tundravm.declarative import lifecycle

        setattr(_source, "fetch_source", self.fetch_source)
        setattr(_source, "_git_output", self.git_output)
        setattr(lifecycle, "fetch_source", self.fetch_source)
        setattr(lifecycle, "prefetch_deps", self.prefetch_deps)
        return self
