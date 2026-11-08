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

from tundravm.lockfile import LockedToolchain

if TYPE_CHECKING:
    from tundravm._source import Source, SourceBuild

CHECKOUT_FILE = "FAKE-CHECKOUT"
"""The one file a fake checkout holds: the source's url and the pin, as JSON."""
SDK = "9.0.100"
"""The .NET SDK every fake Dotnet build is pinned to."""
SDK_KIND = "dotnet-sdk"


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
    build reuses it. A dependency prefetch records an empty, complete cache, so an offline
    build reuses it too and a second prefetch keeps it; a Go build's toolchain is ``local``
    and a Cargo build's ``debian`` (a fake checkout has no go.mod or rust-toolchain.toml),
    and a Dotnet build's SDK is :data:`SDK` (``lock`` pins it without reading a global.json
    or a release manifest, and the prefetch records it without a download). *fetched* lists
    every checkout made, in order.
    """

    fetched: list[FakeFetch] = field(default_factory=list)

    def lock_dotnet_sdk(self, build: SourceBuild, pin: str, checkout: Path) -> LockedToolchain:
        """The .NET SDK a Dotnet build locks to: always :data:`SDK`."""
        del checkout
        return LockedToolchain(
            name=build.key,
            kind=SDK_KIND,
            source=pin,
            requested=f"{SDK} disable",
            version=SDK,
            url=f"https://example.invalid/dotnet-sdk-{SDK}-linux-x64.tar.gz",
            sha512="0" * 128,
        )

    def fetch_source(self, source: Source, pin: str, dest: Path) -> None:
        """Make a checkout of *source* at *pin* in *dest*: one file naming them."""
        dest.mkdir(parents=True)
        text = json.dumps({"url": source.url, "pin": pin}, sort_keys=True) + "\n"
        (dest / CHECKOUT_FILE).write_text(text, encoding="utf-8")
        self.fetched.append(FakeFetch(source.url, pin, dest))

    def git_output(self, checkout: Path, *args: str) -> str | None:
        """What git prints for *args* in a fake *checkout*: its pin for ``rev-parse``, a clean
        tree otherwise; ``None`` outside a fake checkout."""
        try:
            recorded = json.loads((Path(checkout) / CHECKOUT_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if args[:1] == ("rev-parse",):
            return f"{recorded['pin']}\n"
        return ""

    def prefetch_deps(
        self, build: SourceBuild, pin: str, checkout: Path, root: Path, **kwargs: object
    ) -> str | None:
        """Record an empty, complete dependency cache for *build* under *root*; ``None`` for a
        build without dependencies to prefetch."""
        from tundravm import _source

        del checkout
        marker = build.deps_marker(pin)
        if marker is None or not isinstance(build.build, _source.LanguageBuild):
            return None
        deps = Path(root) / _source.DEPS_DIRNAME
        if (deps / marker).is_file():
            return f"{build.build.deps}: kept"
        (deps / build.build.deps).mkdir(parents=True, exist_ok=True)
        record: dict[str, object] = {
            "build": build.key,
            "checkout": build.pin_dir(pin),
            "spec": build.deps_spec(),
            "contents": {},
        }
        toolchain = build.toolchain_file(pin)
        if toolchain is not None:
            if isinstance(build.build, _source.GoBuild):
                name = _source.GO_TOOLCHAIN_LOCAL
                record["toolchain"], _ = _source._go_toolchain_entries(name, deps)
            elif isinstance(build.build, _source.DotnetBuild):
                sdk = kwargs.get("sdk")
                if not isinstance(sdk, LockedToolchain):
                    sdk = self.lock_dotnet_sdk(build, pin, Path(root))
                name = sdk.version
                record["toolchain"] = _source._dotnet_record(sdk)
            else:
                name = _source.RUST_TOOLCHAIN_DEBIAN
                record["toolchain"] = {"name": name, "reason": _source._RUST_DEBIAN_REASON}
            (deps / toolchain).write_text(f"{name}\n", encoding="utf-8")
        (deps / marker).write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        return f"{build.build.deps}: prefetched"

    def install(self, setattr: Callable[[object, str, object], None]) -> FakeSources:
        """Put this fake in place of the fetch boundary using *setattr* (pytest's
        ``monkeypatch.setattr``, which undoes it after the test)."""
        from tundravm import _source
        from tundravm.declarative import lifecycle

        setattr(_source, "fetch_source", self.fetch_source)
        setattr(_source, "_git_output", self.git_output)
        setattr(lifecycle, "fetch_source", self.fetch_source)
        setattr(lifecycle, "prefetch_deps", self.prefetch_deps)
        setattr(lifecycle, "lock_dotnet_sdk", self.lock_dotnet_sdk)
        return self
