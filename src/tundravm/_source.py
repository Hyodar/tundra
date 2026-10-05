"""Source builds: a fetched source plus a build recipe, pinned through the lockfile.

``declarative.lower`` turns each declarative ``Build`` into a :class:`SourceBuild`
recorded on its profile's state; the internal modules declare theirs directly.

``install=`` lists what lands in the image, in order: :meth:`Install.artifact` is
the recipe's own output, :meth:`Install.file` and :meth:`Install.tree` copy further
paths of the source tree.

``tundravm.lock()`` resolves each source to a pin (``LockedFetch`` entries in
``tundravm.lock``) and the cache key carries it. Under ``nethermind-v1`` the build
hook fetches in the build sandbox: it clones the symbolic ref until the source is
pinned, then fetches exactly that commit. Every other dialect renders the hook
*mounted*: :func:`fetch_source` checks the pinned source out on the host as the
invoking user, into ``<out>/.sources/<name>-<pin[:12]>``, the backend mounts that
directory at ``$SRCDIR/tundravm-sources`` and the hook copies the checkout into the
build tree, so the sandbox never fetches a source. A built kernel's source joins
them as a :class:`KernelSource` named ``kernel`` (``kernel-<variant>`` for a variant
whose kernel source differs): locked, fetched and mounted the same way.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .build_cache import Build, Cache, CacheDecl, CacheDir, CacheFile
from .errors import LockfileError, SourceError, TdxError, ValidationError
from .lockfile.model import LockedFetch

if TYPE_CHECKING:
    from .models import Kernel

COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PLAIN_ENV_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]*$")
_ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar.zst")
SOURCES_DIRNAME = ".sources"
"""Directory of the build output that ``tundravm fetch`` writes the checkouts to."""
SOURCES_MOUNT = "tundravm-sources"
"""Where a mounted hook finds the host-fetched checkouts: ``$SRCDIR/tundravm-sources``."""
FETCH_MARKER = ".tundravm-complete"
"""File :func:`fetch_source`'s caller writes, holding the pin, once a checkout is complete."""
MOUNTED_CACHE_ROOT = "${BUILDDIR:-$BUILDROOT/build}"
"""The build cache of a mounted hook: mkosi's ``$BUILDDIR`` when ``BuildDirectory=`` is
configured (kept across builds), else the build overlay (discarded after the build)."""
DEPS_DIRNAME = "deps"
"""``<out>/.sources/deps``: the dependency caches ``tundravm fetch`` fills with the host's
toolchains (``go``, ``cargo``, ``nuget``), mounted with the checkouts."""
DEPS_BUILD_DIR = ".tundravm-deps"
"""Where a mounted hook copies a dependency cache: ``$BUILDROOT/build/.tundravm-deps/<cache>``
(``/build/.tundravm-deps/<cache>`` inside mkosi-chroot), so the mounted tree stays read-only."""
_NETWORK_TIMEOUT = 60.0
"""Seconds :func:`default_resolver` waits on ``git ls-remote`` or a stalled download."""


@dataclass(frozen=True, slots=True)
class GitSource:
    """A git repository at *ref* (branch, tag, or full 40-hex commit sha)."""

    url: str
    ref: str
    subdir: str | None = field(default=None, kw_only=True)
    submodules: bool = field(default=False, kw_only=True)

    kind: Literal["git"] = field(default="git", init=False, repr=False)

    @property
    def requested(self) -> str:
        """What the recipe asked for: the ref."""
        return self.ref

    @property
    def inline_pin(self) -> str | None:
        """The commit when *ref* already is one, else None."""
        return self.ref if COMMIT_PATTERN.fullmatch(self.ref) else None

    def describe(self) -> str:
        """``git <url> @ <ref>``, as lock errors name the source."""
        return f"git {self.url} @ {self.ref}"

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "url": self.url,
            "ref": self.ref,
            "subdir": self.subdir,
            "submodules": self.submodules,
        }


@dataclass(frozen=True, slots=True)
class HttpSource:
    """A file or source archive at *url*; ``sha256`` pins it inline."""

    url: str
    sha256: str | None = field(default=None, kw_only=True)

    kind: Literal["http"] = field(default="http", init=False, repr=False)

    @property
    def requested(self) -> str:
        return self.url

    @property
    def inline_pin(self) -> str | None:
        return self.sha256

    @property
    def filename(self) -> str:
        return posixpath.basename(self.url.split("?", 1)[0]) or "download"

    def describe(self) -> str:
        """``http <url>``, as lock errors name the source."""
        return f"http {self.url}"

    def to_payload(self) -> dict[str, object]:
        return {"kind": self.kind, "url": self.url, "sha256": self.sha256}


Source = GitSource | HttpSource
Resolver = Callable[[Source], str]
"""Maps a source to its pin: a commit sha for git, a sha256 for http."""


def _assignment(key: str, value: str) -> str:
    if _PLAIN_ENV_VALUE.fullmatch(value):
        return f"{key}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'{key}="{escaped}"'


def _env_assignments(env: Mapping[str, str]) -> str:
    return " ".join(_assignment(key, value) for key, value in env.items())


def _since(default: object) -> dict[str, object]:
    """Field metadata: omit the field from the recipe payload while it holds *default*.

    Recipes declared before the field existed keep their payload, so their
    lockfile section digests stay fresh.
    """
    return {"payload_default": default}


class FrozenMap(Mapping[str, str]):
    """An immutable copy of a ``str -> str`` mapping, in the caller's order.

    Build recipes store ``env`` and ``properties`` as one, so changing the dict
    passed at construction changes nothing; it compares equal to a dict with
    the same items and hashes like one ``frozenset`` of them.
    """

    __slots__ = ("_items", "_lookup")

    def __init__(self, items: Mapping[str, str] | Iterable[tuple[str, str]] = ()) -> None:
        pairs = tuple(items.items() if isinstance(items, Mapping) else items)
        self._items: tuple[tuple[str, str], ...] = pairs
        self._lookup: dict[str, str] = dict(pairs)

    def __getitem__(self, key: str) -> str:
        return self._lookup[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._lookup)

    def __len__(self) -> int:
        return len(self._lookup)

    def __hash__(self) -> int:
        return hash(frozenset(self._lookup.items()))

    def __repr__(self) -> str:
        return f"FrozenMap({self._lookup!r})"

    def __reduce__(self) -> tuple[type[FrozenMap], tuple[tuple[tuple[str, str], ...]]]:
        return (FrozenMap, (self._items,))

    def __copy__(self) -> FrozenMap:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> FrozenMap:
        return self


def _freeze_recipe(build: object, mappings: Sequence[str], sequences: Sequence[str]) -> None:
    """Copy *build*'s *mappings* into :class:`FrozenMap` and its *sequences* into tuples."""
    for name in mappings:
        object.__setattr__(build, name, FrozenMap(getattr(build, name)))
    for name in sequences:
        value = getattr(build, name)
        if not isinstance(value, str | tuple):
            object.__setattr__(build, name, tuple(value))


@dataclass(frozen=True, slots=True, kw_only=True)
class GoBuild:
    """``go build -trimpath`` of *package* into ``<output_dir>/<output>``.

    ``output_dir`` is relative to the source tree. ``mkdir`` creates it before the
    build; ``go build -o`` creates it too, so turning it off only drops the step.
    """

    output: str
    package: str = "./..."
    ldflags: str = "-s -w -buildid="
    tags: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    packages: tuple[str, ...] = ("golang",)
    output_dir: str = field(default="./build", metadata=_since("./build"))
    mkdir: bool = field(default=True, metadata=_since(True))

    kind: Literal["go"] = field(default="go", init=False, repr=False)
    tool: ClassVar[str] = "go"
    """The host executable ``tundravm fetch`` prefetches the dependencies with."""
    deps: ClassVar[str] = "go"
    """The dependency cache, ``<out>/.sources/deps/go``: a ``GOMODCACHE``."""

    def __post_init__(self) -> None:
        _freeze_recipe(self, ("env",), ("tags", "packages"))

    def prefetch(self, cache: str) -> tuple[tuple[str, ...], dict[str, str]]:
        """``go mod download`` into the module cache *cache*, and its environment.

        ``-modcacherw`` keeps the cache removable without ``go clean -modcache``.
        """
        env = {**self.env, "GOMODCACHE": cache, "GOFLAGS": "-mod=mod -modcacherw"}
        return ("go", "mod", "download"), env

    @staticmethod
    def deps_env(cache: str) -> str:
        """The exports pointing the in-build ``go`` at the copied module cache *cache*."""
        return (
            f"export GOMODCACHE={cache} GOFLAGS=-mod=mod && "
            '{ [ "$WITH_NETWORK" != 0 ] || export GOPROXY=off; }'
        )

    @property
    def artifact(self) -> str:
        return posixpath.normpath(f"{self.output_dir}/{self.output}")

    def command(self, workdir: str) -> str:
        env = f"{_env_assignments(self.env)} " if self.env else ""
        tags = f" -tags {','.join(self.tags)}" if self.tags else ""
        mkdir = f"mkdir -p {self.output_dir} && " if self.mkdir else ""
        return (
            f"cd {workdir} && {mkdir}{env}go build -trimpath{tags} "
            f'-ldflags "{self.ldflags}" -o {self.output_dir}/{self.output} {self.package}'
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CargoBuild:
    """``cargo fetch && cargo build --frozen``; *bin* or *package* selects the target."""

    output: str
    bin: str | None = None
    package: str | None = None
    features: tuple[str, ...] = ()
    profile: str = "release"
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    packages: tuple[str, ...] = ("cargo",)

    kind: Literal["cargo"] = field(default="cargo", init=False, repr=False)
    tool: ClassVar[str] = "cargo"
    """The host executable ``tundravm fetch`` prefetches the dependencies with."""
    deps: ClassVar[str] = "cargo"
    """The dependency cache, ``<out>/.sources/deps/cargo``: a ``CARGO_HOME`` registry cache."""

    def __post_init__(self) -> None:
        _freeze_recipe(self, ("env",), ("features", "packages"))

    def prefetch(self, cache: str) -> tuple[tuple[str, ...], dict[str, str]]:
        """``cargo fetch --locked`` into the cargo home *cache*, and its environment."""
        return ("cargo", "fetch", "--locked"), {**self.env, "CARGO_HOME": cache}

    @staticmethod
    def deps_env(cache: str) -> str:
        """The exports pointing the in-build ``cargo`` at the copied cargo home *cache*."""
        return (
            f"export CARGO_HOME={cache} && "
            '{ [ "$WITH_NETWORK" != 0 ] || export CARGO_NET_OFFLINE=true; }'
        )

    @property
    def artifact(self) -> str:
        return f"target/{self.profile}/{self.bin or self.package or self.output}"

    def command(self, workdir: str) -> str:
        env = f"export {_env_assignments(self.env)} && " if self.env else ""
        profile = "--release" if self.profile == "release" else f"--profile {self.profile}"
        flags = ""
        if self.features:
            flags += f" --features {','.join(self.features)}"
        if self.package:
            flags += f" --package {self.package}"
        if self.bin:
            flags += f" --bin {self.bin}"
        return f"{env}cd {workdir} && cargo fetch && cargo build {profile} --frozen{flags}"


@dataclass(frozen=True, slots=True, kw_only=True)
class DotnetBuild:
    """Deterministic self-contained ``dotnet publish`` of *project* into ``publish/``.

    ``restore_args`` are appended to ``dotnet restore``; ``properties`` become extra
    ``-p:<name>=<value>`` publish properties after the deterministic ones.
    """

    project: str
    output: str
    configuration: str = "Release"
    runtime: str = "linux-x64"
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    packages: tuple[str, ...] = ("dotnet-sdk-8.0",)
    restore_args: tuple[str, ...] = field(default=(), metadata=_since(()))
    properties: Mapping[str, str] = field(default_factory=FrozenMap, metadata=_since({}))

    kind: Literal["dotnet"] = field(default="dotnet", init=False, repr=False)
    tool: ClassVar[str] = "dotnet"
    """The host executable ``tundravm fetch`` prefetches the dependencies with."""
    deps: ClassVar[str] = "nuget"
    """The dependency cache, ``<out>/.sources/deps/nuget``: a NuGet global packages folder."""

    def __post_init__(self) -> None:
        _freeze_recipe(self, ("env", "properties"), ("packages", "restore_args"))

    def prefetch(self, cache: str) -> tuple[tuple[str, ...], dict[str, str]]:
        """``dotnet restore`` of *project* for *runtime* into the packages folder *cache*."""
        argv = (
            "dotnet",
            "restore",
            self.project,
            "--runtime",
            self.runtime,
            *self.restore_args,
            "--packages",
            cache,
        )
        env = {**self.env, "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"}
        return argv, env

    @staticmethod
    def deps_env(cache: str) -> str:
        """The exports pointing the in-build ``dotnet restore`` at the copied packages *cache*.

        Without network, MSBuild reads ``RestoreSources`` from the environment, so
        restore (and ``publish``'s implicit one) resolves from *cache* alone.
        """
        return (
            f"export NUGET_PACKAGES={cache} && "
            f'{{ [ "$WITH_NETWORK" != 0 ] || export RestoreSources={cache}; }}'
        )

    @property
    def artifact(self) -> str:
        return f"publish/{self.output}"

    def command(self, workdir: str) -> str:
        env = f"export {_env_assignments(self.env)} && " if self.env else ""
        restore = "".join(f" {arg}" for arg in self.restore_args)
        props = "".join(f" -p:{_assignment(k, v)}" for k, v in self.properties.items())
        return (
            f"{env}cd {workdir} && dotnet restore {self.project} --runtime {self.runtime}"
            f"{restore} && dotnet publish {self.project} --configuration {self.configuration} "
            f"--runtime {self.runtime} --self-contained true --output {workdir}/publish "
            f"-p:Deterministic=true -p:ContinuousIntegrationBuild=true{props}"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ScriptBuild:
    """Run *script* in the source tree; *output* is the produced file, relative to it.

    *env* is exported before the script runs.
    """

    script: str
    output: str
    packages: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=FrozenMap, metadata=_since({}))

    kind: Literal["script"] = field(default="script", init=False, repr=False)

    def __post_init__(self) -> None:
        _freeze_recipe(self, ("env",), ("packages",))

    @property
    def artifact(self) -> str:
        return self.output

    def command(self, workdir: str) -> str:
        env = f"export {_env_assignments(self.env)} && " if self.env else ""
        return f"{env}cd {workdir} && {self.script}"


BuildRecipe = GoBuild | CargoBuild | DotnetBuild | ScriptBuild
LanguageBuild = GoBuild | CargoBuild | DotnetBuild
"""The build recipes whose dependencies ``tundravm fetch`` prefetches on the host."""


def _recipe_payload(build: BuildRecipe) -> dict[str, object]:
    payload: dict[str, object] = {"kind": build.kind}
    for item in fields(build):
        if item.name == "kind":
            continue
        value = getattr(build, item.name)
        if "payload_default" in item.metadata and value == item.metadata["payload_default"]:
            continue
        if isinstance(value, Mapping):
            value = dict(sorted(value.items()))
        elif isinstance(value, tuple):
            value = list(value)
        payload[item.name] = value
    return payload


InstallKind = Literal["artifact", "file", "tree"]


@dataclass(frozen=True, slots=True)
class Install:
    """One install step of a :class:`SourceBuild`; build it with a constructor below.

    ``artifact`` installs the recipe's own output, ``file`` one more file of the
    source tree and ``tree`` a whole directory of it (the built files keep their
    modes). *path* is relative to the source tree, *dest* absolute.
    """

    kind: InstallKind
    dest: str
    path: str | None = None
    mode: str | None = None

    @classmethod
    def artifact(cls, dest: str, *, mode: str = "0755") -> Install:
        """The build recipe's artifact, installed at *dest* with *mode*."""
        return cls(kind="artifact", dest=dest, mode=mode)

    @classmethod
    def file(cls, path: str, dest: str, *, mode: str = "0755") -> Install:
        """The file *path* of the source tree, installed at *dest* with *mode*."""
        return cls(kind="file", dest=dest, path=path, mode=mode)

    @classmethod
    def tree(cls, path: str, dest: str) -> Install:
        """The directory *path* of the source tree, copied to *dest*."""
        return cls(kind="tree", dest=dest, path=path)

    def __post_init__(self) -> None:
        if self.kind not in ("artifact", "file", "tree"):
            raise ValidationError(
                f"Unknown install kind {self.kind!r}.",
                hint="Use Install.artifact(), Install.file() or Install.tree().",
            )
        if self.kind == "artifact" and self.path is not None:
            raise ValidationError(
                "Install.artifact() takes no path: it installs the build's artifact.",
                hint="Use Install.file(path, dest) to install a file from the source tree.",
                context={"path": self.path},
            )
        if self.kind == "tree" and self.mode is not None:
            raise ValidationError(
                f"tree {self.path!r} takes no mode.",
                hint="Directory copies keep the built files' modes.",
            )
        if self.kind == "file" and (self.path or "").endswith("/"):
            raise ValidationError(
                f"file {self.path!r} names a directory.",
                hint="Use Install.tree() to copy a directory.",
            )
        if self.kind != "artifact":
            path = self.path or ""
            if not path or path.startswith("/") or ".." in path.rstrip("/").split("/"):
                raise ValidationError(
                    f"install path {path!r} must be relative to the source tree.",
                    hint="Name it from the checkout root, without '/' or '..', e.g. 'bin/app'.",
                    context={"path": path},
                )
        if not self.dest.startswith("/"):
            raise ValidationError(
                f"install destination {self.dest!r} must be absolute.",
                hint="Pass the path in the image, e.g. '/usr/local/bin/app'.",
                context={"path": self.path or "", "dest": self.dest},
            )


class NamedSource:
    """A source under its lockfile name: what ``lock`` pins and ``fetch`` checks out.

    :class:`SourceBuild` and :class:`KernelSource` share the pin bookkeeping:
    the lockfile entry is ``fetches[key]``, the host checkout
    ``.sources/<name>-<pin[:12]>-<identity[:8]>`` (:meth:`pin_dir`). The *key* is
    the name, or ``<variant>/<name>`` (``lock_name``) for a build whose source
    differs between variants.
    """

    __slots__ = ()
    name: str
    source: Source
    lock_name: str | None

    @property
    def key(self) -> str:
        """The lockfile name of this source: ``lock_name``, else ``name``."""
        return self.lock_name or self.name

    @property
    def mutable(self) -> bool:
        """True when the declaration alone does not pin the source."""
        return self.source.inline_pin is None

    def pin_from(self, fetches: Mapping[str, LockedFetch]) -> str | None:
        """This source's pin: inline, or a lockfile entry recorded for the same source."""
        if self.source.inline_pin is not None:
            return self.source.inline_pin
        locked = fetches.get(self.key)
        if locked is None or not self.matches(locked):
            return None
        return locked.digest

    def matches(self, locked: LockedFetch) -> bool:
        """True when *locked* was resolved from exactly this source declaration."""
        ref = self.source.ref if isinstance(self.source, GitSource) else None
        return (
            locked.kind == self.source.kind
            and locked.source == self.source.url
            and locked.ref == ref
        )

    def locked(self, digest: str) -> LockedFetch:
        ref = self.source.ref if isinstance(self.source, GitSource) else None
        return LockedFetch(
            source=self.source.url, kind=self.source.kind, digest=digest, name=self.key, ref=ref
        )

    def pin_dir(self, pin: str) -> str:
        """``<name>-<pin[:12]>-<identity[:8]>``: the ``.sources`` directory of *pin*'s checkout.

        The identity (:func:`checkout_identity`) covers what the checkout holds
        besides the pin: url, and for git the subdirectory and submodules.
        """
        return f"{self.name}-{pin[:12]}-{checkout_identity(self.source)[:8]}"


@dataclass(frozen=True, slots=True)
class KernelSource(NamedSource):
    """The source of a built kernel (one with a config), named ``kernel`` or ``kernel-<variant>``.

    Outside ``nethermind-v1`` the kernel build script copies its host-fetched
    checkout from ``$SRCDIR/tundravm-sources/<name>-<pin[:12]>``.
    """

    name: str
    source: Source
    version: str
    lock_name: str | None = field(default=None, compare=False, repr=False, kw_only=True)

    @classmethod
    def of(cls, kernel: Kernel, name: str = "kernel") -> KernelSource:
        """*kernel*'s source: its tarball, or its repository at ``source_ref`` (``v<version>``)."""
        version = kernel.version or "unknown"
        if kernel.source_archive is not None:
            source: Source = HttpSource(kernel.source_archive, sha256=kernel.source_sha256)
        else:
            source = GitSource(
                kernel.source_repo,
                kernel.source_ref or f"v{version}",
                subdir=kernel.source_subdir,
                submodules=kernel.source_submodules,
            )
        return cls(name, source, version)


@dataclass(frozen=True, slots=True)
class DebFile(NamedSource):
    """A ``.deb`` a postinst hook installs with ``dpkg -i``: :class:`EfiStub`'s package.

    Outside ``nethermind-v1`` it is a source like the others: ``lock`` pins its
    sha256 as ``fetches[name]``, ``fetch`` downloads it on the host into
    ``.sources/<name>-<pin[:12]>-<id[:8]>`` and the backend mounts it. *script* is
    the hook compiled without a pin, which downloads it in the build sandbox;
    :meth:`render` with a pin installs the mounted, sha256-checked copy instead.
    """

    name: str
    source: HttpSource
    script: str = field(repr=False)
    lock_name: str | None = field(default=None, compare=False, repr=False, kw_only=True)

    def render(self, pin: str | None) -> str:
        """The postinst hook: *script* unpinned, else ``dpkg -i`` of the mounted copy."""
        effective = self.source.inline_pin or pin
        if effective is None:
            return self.script
        directory = f"{SOURCES_MOUNT}/{self.pin_dir(effective)}"
        deb = f"{directory}/{self.source.filename}"
        missing = f"tundravm: {self.pin_dir(effective)} is not fetched: run tundravm fetch RECIPE"
        return (
            f'if [ ! -f "$SRCDIR/{directory}/{FETCH_MARKER}" ]; then\n'
            f"    echo {shlex.quote(missing)} >&2\n"
            "    exit 1\n"
            "fi\n"
            f'echo "{effective}  $SRCDIR/{deb}" | sha256sum -c --quiet -\n'
            f'mkosi-chroot dpkg -i "$CHROOT_SRCDIR/{deb}"'
        )


@dataclass(frozen=True, slots=True)
class SourceBuild(NamedSource):
    """Fetch *source*, run *build* inside mkosi-chroot, install the results.

    *install* lists the :class:`Install` steps; artifacts are cached under their
    destination's file name and installed in that order.

    A mounted (current-dialect) hook caches under ``<namespace>-<fingerprint>``:
    the namespace is ``cache_key`` (default: the build name) and the
    fingerprint (:meth:`cache_fingerprint`) covers the pin, the build and install
    specs, the toolchain and the distribution it comes from, so changing any of
    them rebuilds.
    Under ``nethermind-v1`` ``cache_key`` replaces the default
    ``<name>-<url sha256[:12]>-<ref>`` key and a lockfile pin appends
    ``-<pin[:12]>`` to it (default key: replaces the ref). ``mark_unpinned``
    prefixes the unpinned hook with ``# unpinned: <ref>``; modules that must keep
    the exact hook bytes they emitted before source builds existed turn it off.
    """

    name: str
    source: Source
    build: BuildRecipe
    install: tuple[Install, ...] = ()
    cache_key: str | None = None
    mark_unpinned: bool = True
    lock_name: str | None = field(default=None, compare=False, repr=False, kw_only=True)

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.fullmatch(self.name):
            raise ValidationError(
                f"Invalid source build name {self.name!r}.",
                hint="Use letters, digits, '.', '_' or '-'.",
            )
        object.__setattr__(self, "install", tuple(self.install))
        if not self.install:
            raise ValidationError(
                f"source build {self.name!r} installs nothing.",
                hint="Pass install=(Install.artifact('/usr/bin/<name>'), ...).",
            )
        names = [posixpath.basename(dest.rstrip("/")) for _, dest, _, _ in self._targets()]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValidationError(
                f"source build {self.name!r}: install destinations share the file name "
                f"{', '.join(duplicates)}.",
                hint="Each artifact is cached under its destination's file name.",
            )
        if isinstance(self.source, GitSource) and not self.source.ref:
            raise ValidationError(
                f"source build {self.name!r}: GitSource requires a ref.",
                hint="Give Git() a tag, branch or commit; `tundravm lock` pins it.",
            )
        if isinstance(self.source, HttpSource) and self.source.sha256 is not None:
            if not SHA256_PATTERN.fullmatch(self.source.sha256):
                raise ValidationError(
                    f"source build {self.name!r}: sha256 must be 64 lowercase hex chars.",
                    hint="Give Http() the `sha256sum FILE` output, or omit it and `tundravm lock`.",
                )

    def _targets(self) -> list[tuple[str, str, str, bool]]:
        """``(path in the source tree, dest, mode, is_dir)`` per artifact, in order."""
        return [
            (
                self.build.artifact if step.path is None else step.path,
                step.dest,
                step.mode or "",
                step.kind == "tree",
            )
            for step in self.install
        ]

    @property
    def packages(self) -> tuple[str, ...]:
        """Build packages the source fetch and the build recipe need."""
        fetch = ("git",) if isinstance(self.source, GitSource) else ("curl",)
        return tuple(dict.fromkeys((*fetch, *self.build.packages)))

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source": self.source.to_payload(),
            "build": _recipe_payload(self.build),
            "install": [
                {"kind": step.kind, "path": step.path, "dest": step.dest, "mode": step.mode}
                for step in self.install
            ],
            "cache_key": self.cache_key,
            "mark_unpinned": self.mark_unpinned,
        }

    def render(
        self,
        pin: str | None = None,
        *,
        mounted: bool = False,
        distribution: Mapping[str, object] | None = None,
    ) -> str:
        """The build-phase hook: fetch, build, cache, install.

        *pin* is a lockfile pin; an inline pin (commit ref, ``sha256=``) wins.
        *mounted* copies the host-fetched checkout ``$SRCDIR/tundravm-sources/
        <name>-<pin[:12]>`` instead of fetching in the sandbox; unpinned, the
        mounted hook only fails, naming ``tundravm lock`` and ``tundravm fetch``.
        *distribution* (the variant's ``Lowered.build_distribution``) enters the
        mounted hook's cache fingerprint.
        """
        effective = self.source.inline_pin or pin
        workdir = Build.chroot_path(self.name).rel
        if isinstance(self.source, GitSource) and self.source.subdir:
            workdir = f"{workdir}/{self.source.subdir.strip('/')}"
        build = self.build.command(f"/build/{workdir}")
        inner = build.replace("'", "'\\''")
        if mounted:
            if effective is None:
                message = (
                    f"tundravm: source build {self.name} is not pinned: "
                    "run tundravm lock, then tundravm fetch"
                )
                return (
                    f"# unpinned: {self.source.requested}\n"
                    f"echo {shlex.quote(message)} >&2 && exit 1"
                )
            copy_deps, uses_deps = self._deps()
            inner = f"{uses_deps}{build}".replace("'", "'\\''")
            command = f"{self._copy(effective)}{copy_deps} && mkosi-chroot bash -c '{inner}'"
            fingerprint = self.cache_fingerprint(effective, distribution=distribution or {})
            key = f"{self.cache_key or self.name}-{fingerprint}"
            return self._cache(key, workdir).wrap(
                command, root=MOUNTED_CACHE_ROOT, exact_trees=True
            )
        command = f"{self._fetch(effective)} && mkosi-chroot bash -c '{inner}'"
        hook = self._cache(self._historical_key(effective), workdir).wrap(command)
        if effective is None and self.mark_unpinned:
            return f"# unpinned: {self.source.requested}\n{hook}"
        return hook

    def _copy(self, pin: str) -> str:
        """Copy the mounted checkout of *pin* to the build tree, without its marker."""
        source = f'"$SRCDIR/{SOURCES_MOUNT}/{self.pin_dir(pin)}"'
        target = f'"{Build.build_path(self.name)}"'
        missing = f"tundravm: {self.pin_dir(pin)} is not fetched: run tundravm fetch RECIPE"
        return (
            f"[ -f {source}/{FETCH_MARKER} ] || {{ echo {shlex.quote(missing)} >&2; exit 1; }}"
            f" && mkdir -p {target} && cp -a --no-preserve=ownership {source}/. {target}/"
            f" && rm -f {target}/{FETCH_MARKER}"
        )

    def _deps(self) -> tuple[str, str]:
        """``(copy, exports)``: a mounted hook's use of the prefetched dependency cache.

        *copy* (``&&``-joined after the checkout copy) copies
        ``$SRCDIR/tundravm-sources/deps/<cache>`` to ``$BUILDROOT/build/.tundravm-deps``
        once, when ``tundravm fetch`` filled it; *exports* (inside mkosi-chroot) point
        the toolchain at that copy, offline when mkosi runs the build without network.
        Both are empty for a script build.
        """
        if not isinstance(self.build, LanguageBuild):
            return "", ""
        cache = self.build.deps
        mounted = f'"$SRCDIR/{SOURCES_MOUNT}/{DEPS_DIRNAME}/{cache}"'
        parent = f'"{Build.build_path(DEPS_BUILD_DIR)}"'
        target = f'"{Build.build_path(f"{DEPS_BUILD_DIR}/{cache}")}"'
        copy = (
            f" && {{ [ ! -d {mounted} ] || [ -d {target} ] || "
            f"{{ mkdir -p {parent} && cp -a --no-preserve=ownership {mounted} {target}; }}; }}"
        )
        chroot = Build.chroot_path(f"{DEPS_BUILD_DIR}/{cache}")
        return copy, f"if [ -d {chroot} ]; then {self.build.deps_env(str(chroot))}; fi && "

    def deps_marker(self, pin: str) -> str | None:
        """``<pin_dir>.<cache>.json``: the file in ``.sources/deps`` that records a prefetch.

        ``tundravm fetch`` writes it once the host toolchain filled the cache for
        *pin*'s checkout; ``None`` for a script build, which has no dependency cache.
        """
        if not isinstance(self.build, LanguageBuild):
            return None
        return f"{self.pin_dir(pin)}.{self.build.deps}.json"

    def deps_spec(self) -> dict[str, object]:
        """What a prefetch ran, as its marker records it: toolchain, command and environment."""
        if not isinstance(self.build, LanguageBuild):
            return {}
        argv, env = self.build.prefetch(f"<{DEPS_DIRNAME}/{self.build.deps}>")
        return {
            "toolchain": self.build.kind,
            "command": list(argv),
            "env": dict(sorted(env.items())),
        }

    def cache_fingerprint(self, pin: str, *, distribution: Mapping[str, object]) -> str:
        """The first 16 hex of the sha256 over what a mounted build produces.

        Canonical JSON of the source pin (with git's ``subdir`` and
        ``submodules``), the build recipe and install steps as the lockfile
        records them, the toolchain (recipe kind and packages) and the
        *distribution* it comes from: base, architecture, mirror, snapshot,
        repositories, build packages and build settings
        (``Lowered.build_distribution``).
        """
        source: dict[str, object] = {"kind": self.source.kind, "pin": pin}
        if isinstance(self.source, GitSource):
            source.update(subdir=self.source.subdir, submodules=self.source.submodules)
        spec = {
            "source": source,
            "build": _recipe_payload(self.build),
            "install": [
                {"kind": step.kind, "path": step.path, "dest": step.dest, "mode": step.mode}
                for step in self.install
            ],
            "distribution": dict(distribution),
            "toolchain": {"kind": self.build.kind, "packages": list(self.packages)},
        }
        canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def _historical_key(self, pin: str | None) -> str:
        """The ``nethermind-v1`` cache key: ``cache_key`` or name, url digest and ref, + pin."""
        if self.cache_key is not None:
            return self.cache_key if pin is None else f"{self.cache_key}-{pin[:12]}"
        url_hash = hashlib.sha256(self.source.url.encode("utf-8")).hexdigest()[:12]
        if pin is not None:
            version = pin[:12]
        elif isinstance(self.source, GitSource):
            version = self.source.ref
        else:
            version = "unpinned"
        return f"{self.name}-{url_hash}-{version}"

    def _cache(self, key: str, workdir: str) -> CacheDecl:
        artifacts: list[CacheFile | CacheDir] = []
        for path, dest, mode, is_dir in self._targets():
            src = Build.build_path(f"{workdir}/{path.rstrip('/')}")
            target = Build.dest_path(dest.rstrip("/").lstrip("/"))
            name = posixpath.basename(dest.rstrip("/"))
            if is_dir:
                artifacts.append(Cache.dir(src=src, dest=target, name=name))
            else:
                artifacts.append(Cache.file(src=src, dest=target, name=name, mode=mode))
        return Cache.declare(key, tuple(artifacts))

    def _fetch(self, pin: str | None) -> str:
        target = f'"{Build.build_path(self.name)}"'
        if isinstance(self.source, HttpSource):
            return self._fetch_http(pin, target)
        repo = shlex.quote(self.source.url)
        if pin is None:
            ref = shlex.quote(self.source.ref)
            if self.source.submodules:
                return (
                    "git clone --depth=1 --recurse-submodules --shallow-submodules "
                    f"-b {ref} {repo} {target}"
                )
            return f"git clone --depth=1 -b {ref} {repo} {target}"
        command = (
            f"git init -q {target} && git -C {target} fetch -q --depth=1 {repo} {pin} && "
            f"git -C {target} checkout -q FETCH_HEAD"
        )
        if self.source.submodules:
            command += f" && git -C {target} submodule update -q --init --recursive --depth=1"
        return command

    def _fetch_http(self, pin: str | None, target: str) -> str:
        assert isinstance(self.source, HttpSource)
        file = f'"{Build.build_path(f"{self.name}/{self.source.filename}")}"'
        command = f"mkdir -p {target} && curl -fsSL {shlex.quote(self.source.url)} -o {file}"
        if pin is not None:
            command += f' && echo "{pin}  "{file} | sha256sum -c -'
        if self.source.filename.endswith(_ARCHIVE_SUFFIXES):
            command += f" && tar -xf {file} -C {target} --strip-components=1"
        return command


def checkout_identity(source: Source) -> str:
    """The sha256 of what a checkout of *source* holds besides its pin.

    The url and kind, and for git the subdirectory and whether submodules are
    checked out; the ref is left out, since one pin checks out the same tree
    whichever ref resolved to it.
    """
    payload = _identity_payload(source)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _identity_payload(source: Source) -> dict[str, object]:
    if isinstance(source, GitSource):
        return {
            "kind": source.kind,
            "url": source.url,
            "subdir": source.subdir,
            "submodules": source.submodules,
        }
    return {"kind": source.kind, "url": source.url}


def write_marker(checkout: Path, source: Source, pin: str) -> None:
    """Mark *checkout* complete: its pin, its source identity and, for http, a file manifest.

    :func:`is_fetched` verifies the checkout against it before reuse.
    """
    marker: dict[str, object] = {"pin": pin, "source": _identity_payload(source)}
    if isinstance(source, HttpSource):
        marker["files"] = _manifest(checkout)
    text = json.dumps(marker, indent=2, sort_keys=True) + "\n"
    (checkout / FETCH_MARKER).write_text(text, encoding="utf-8")


def _read_marker(checkout: Path) -> dict[str, object] | None:
    try:
        raw = (checkout / FETCH_MARKER).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        marker = json.loads(raw)
    except ValueError:
        return None
    return marker if isinstance(marker, dict) else None


def _manifest(root: Path) -> dict[str, str]:
    """``{path: sha256[ x] | -> target}`` of every file and symlink under *root* but the marker."""
    found: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        dirnames.sort()
        for name in sorted([*filenames, *(d for d in dirnames if (base / d).is_symlink())]):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if rel == FETCH_MARKER:
                continue
            if path.is_symlink():
                found[rel] = f"-> {os.readlink(path)}"
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            found[rel] = digest + (" x" if path.stat().st_mode & 0o111 else "")
    return found


def is_fetched(
    checkout: Path, pin: str, source: Source | None = None, *, name: str | None = None
) -> bool:
    """Whether *checkout* holds a complete, unmodified fetch of *pin*.

    ``False`` when it was never completed (no marker naming *pin*): fetching it
    again replaces it. With *source*, a completed checkout is also verified:
    git ``HEAD`` is the pin, the tree is clean (``git status`` lists nothing,
    ignored and untracked files included) and, when the source asks for them,
    the submodules are initialised at their recorded commits; an http checkout
    still matches the manifest written when it was fetched. A mismatch raises
    :class:`~tundravm.errors.SourceError` naming *name* and the repair command.
    """
    marker = _read_marker(checkout)
    if marker is None or marker.get("pin") != pin:
        return False
    if source is None:
        return True
    problem = _checkout_problem(checkout, source, pin, marker)
    if problem is None:
        return True
    label = name or checkout.name
    raise SourceError(
        f"source {label} checkout modified/incomplete: run tundravm fetch --force",
        source=source.describe(),
        reason=problem,
        hint=(
            f"{problem}. Run `tundravm fetch RECIPE --force` to check {label} out again "
            "(it replaces the checkout)."
        ),
        context={"path": str(checkout)},
    )


def _checkout_problem(
    checkout: Path, source: Source, pin: str, marker: Mapping[str, object]
) -> str | None:
    """Why *checkout* no longer holds *source* at *pin*, or ``None`` when it does."""
    if marker.get("source") != _identity_payload(source):
        return "the checkout was fetched for another source declaration"
    if isinstance(source, HttpSource):
        recorded = marker.get("files")
        if not isinstance(recorded, dict):
            return "the checkout has no file manifest"
        current = _manifest(checkout)
        changed = sorted(
            path
            for path in recorded.keys() | current.keys()
            if recorded.get(path) != current.get(path)
        )
        return None if not changed else _changed_files(changed)
    head = _git_output(checkout, "rev-parse", "HEAD")
    if head is None:
        return "it is not a git checkout"
    if head.strip() != pin:
        # an annotated tag's pin names the tag object; HEAD is the commit it tags
        tagged = _git_output(checkout, "rev-parse", "--verify", "-q", f"{pin}^{{commit}}")
        if tagged is None or tagged.strip() != head.strip():
            return f"HEAD is {head.strip()[:12]}, not the pin {pin[:12]}"
    status = _git_output(
        checkout,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignored",
        "--",
        ".",
        f":(exclude){FETCH_MARKER}",
    )
    if status is None:
        return "git status failed"
    changed = [line[3:] for line in status.splitlines() if line.strip()]
    if changed:
        return _changed_files(changed)
    if source.submodules:
        listing = _git_output(checkout, "submodule", "status", "--recursive")
        if listing is None:
            return "git submodule status failed"
        stale = [line[1:].split()[1] for line in listing.splitlines() if line[:1] in "-+U"]
        if stale:
            return f"submodules not initialised at their recorded commits: {', '.join(stale)}"
    return None


def _changed_files(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:3])
    more = f" and {len(paths) - 3} more" if len(paths) > 3 else ""
    noun = "file" if len(paths) == 1 else "files"
    return f"{len(paths)} {noun} changed since the fetch: {shown}{more}"


def _git_output(checkout: Path, *args: str) -> str | None:
    """git's stdout for *args* run in *checkout*, or ``None`` when git fails."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=False,
            text=True,
            capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError:
        return None
    return completed.stdout if completed.returncode == 0 else None


def fetch_source(source: Source, pin: str, dest: Path) -> None:
    """Check *source* out at *pin* into the new directory *dest*, as the invoking user.

    git: ``git init`` + ``fetch --depth=1 <url> <pin>`` + ``checkout FETCH_HEAD``
    (and the submodules when the source asks for them); git never prompts, so
    its credential helpers and SSH agent apply. http: the download, checked
    against *pin* and unpacked (``--strip-components=1``) next to the archive
    when it is a tarball, as the in-sandbox hook did. Raises
    :class:`~tundravm.errors.SourceError` naming the source and the reason.
    """
    dest.mkdir(parents=True)
    if isinstance(source, HttpSource):
        _download(source, pin, dest)
        return
    _git(source, "init", "-q", str(dest))
    _git(source, "-C", str(dest), "fetch", "-q", "--depth=1", source.url, pin)
    _git(source, "-C", str(dest), "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")
    if source.submodules:
        _git(
            source,
            "-C",
            str(dest),
            "submodule",
            "update",
            "-q",
            "--init",
            "--recursive",
            "--depth=1",
        )


def run_tool(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run a host toolchain command for :func:`prefetch_deps` (tests replace it)."""
    return subprocess.run(
        list(argv), cwd=cwd, env=dict(env), check=False, text=True, capture_output=True
    )


NOT_PREFETCHED = "not prefetched"
""":func:`deps_problem` of a build whose dependencies no fetch has recorded."""


def deps_problem(build: SourceBuild, pin: str, root: Path) -> str | None:
    """Why ``<root>/deps`` lacks *build*'s dependencies for *pin*, or ``None`` when complete.

    *root* is ``<out>/.sources``. Complete means the marker records this
    prefetch command and every entry of its content manifest is still there:
    each Go module directory and ``.mod`` file ``go.sum`` names, each ``.crate``
    of ``Cargo.lock`` (size and sha256), each NuGet package directory of the
    restore's ``project.assets.json``. ``None`` for a script build, which has none;
    :data:`NOT_PREFETCHED` without a marker.
    """
    marker = build.deps_marker(pin)
    if marker is None or not isinstance(build.build, LanguageBuild):
        return None
    try:
        recorded = json.loads((root / DEPS_DIRNAME / marker).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NOT_PREFETCHED
    if not isinstance(recorded, dict):
        return NOT_PREFETCHED
    if recorded.get("spec") != build.deps_spec():
        return "prefetched by another toolchain command"
    contents = recorded.get("contents")
    if not isinstance(contents, dict):
        return "the marker has no content manifest"
    cache = root / DEPS_DIRNAME / build.build.deps
    label = f"{DEPS_DIRNAME}/{build.build.deps}"
    if not cache.is_dir():
        return f"{label} is missing"
    for rel, expected in sorted(contents.items()):
        if _cache_entry(cache / rel) != expected:
            return f"{label}/{rel} is missing or changed"
    return None


def deps_prefetched(build: SourceBuild, pin: str, root: Path) -> bool:
    """Whether ``<root>/deps`` holds *build*'s dependencies for *pin* (:func:`deps_problem`)."""
    return deps_problem(build, pin, root) is None


def _cache_entry(path: Path) -> str | None:
    """A content-manifest value: ``dir`` (non-empty directory) or ``<size> <sha256>``."""
    if path.is_dir():
        return "dir" if any(path.iterdir()) else None
    if not path.is_file():
        return None
    return f"{path.stat().st_size} {hashlib.sha256(path.read_bytes()).hexdigest()}"


def _go_escape(text: str) -> str:
    """The module cache's case encoding: an upper-case letter becomes ``!`` + lower case."""
    return re.sub(r"[A-Z]", lambda match: "!" + match.group(0).lower(), text)


def _go_entries(workdir: Path) -> list[str]:
    """The module directories and ``.mod`` files ``go.sum`` in *workdir* names."""
    try:
        lines = (workdir / "go.sum").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    found: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) != 3:
            continue
        module, version = _go_escape(parts[0]), _go_escape(parts[1])
        if version.endswith("/go.mod"):
            found.append(f"cache/download/{module}/@v/{version.removesuffix('/go.mod')}.mod")
        else:
            found.append(f"{module}@{version}")
    return found


def _cargo_entries(workdir: Path, tree: Path, cache: Path) -> list[str]:
    """The registry ``.crate`` files of the ``Cargo.lock`` at or above *workdir*."""
    for directory in (workdir, *workdir.parents):
        lock = directory / "Cargo.lock"
        if lock.is_file() or directory == tree:
            break
    try:
        packages = tomllib.loads(lock.read_text(encoding="utf-8")).get("package", [])
    except (OSError, ValueError):
        return []
    found: list[str] = []
    for package in packages if isinstance(packages, list) else []:
        if not isinstance(package, dict):
            continue
        if not str(package.get("source", "")).startswith(("registry+", "sparse+")):
            continue
        crate = f"{package.get('name')}-{package.get('version')}.crate"
        found.extend(
            path.relative_to(cache).as_posix()
            for path in sorted(cache.glob(f"registry/cache/*/{crate}"))
        )
    return found


def _nuget_entries(workdir: Path) -> list[str]:
    """The package directories the restore's ``obj/project.assets.json`` files list."""
    found: list[str] = []
    for assets in sorted(workdir.rglob("obj/project.assets.json")):
        try:
            libraries = json.loads(assets.read_text(encoding="utf-8")).get("libraries", {})
        except (OSError, ValueError):
            continue
        for library in libraries.values() if isinstance(libraries, dict) else []:
            if isinstance(library, dict) and library.get("type") == "package":
                found.append(str(library.get("path", "")).lower().rstrip("/"))
    return found


def _deps_contents(recipe: LanguageBuild, workdir: Path, tree: Path, cache: Path) -> dict[str, str]:
    """The content manifest of a prefetch into *cache*: what it holds for this build.

    Entries the toolchain did not put in the cache (a module the build list
    prunes) are left out.
    """
    if isinstance(recipe, GoBuild):
        names = _go_entries(workdir)
    elif isinstance(recipe, CargoBuild):
        names = _cargo_entries(workdir, tree, cache)
    else:
        names = _nuget_entries(workdir)
    contents: dict[str, str] = {}
    for rel in names:
        if rel and ".." not in rel.split("/"):
            entry = _cache_entry(cache / rel)
            if entry is not None:
                contents[rel] = entry
    return dict(sorted(contents.items()))


def prefetch_deps(
    build: SourceBuild,
    pin: str,
    checkout: Path,
    root: Path,
    *,
    offline: bool = False,
    force: bool = False,
    clear: bool = False,
) -> str | None:
    """Fill ``<root>/deps/<cache>`` with *build*'s dependencies; the outcome, for the notice.

    The host's toolchain (``go mod download``, ``cargo fetch --locked``, ``dotnet
    restore --packages``) runs as the invoking user in a scratch copy of
    *checkout* (subdirectory included), so the checkout stays clean, and a marker
    (:meth:`SourceBuild.deps_marker`) records the success with a content manifest
    (:func:`deps_problem`). Returns ``None`` for a script build, ``"<cache>: kept"``
    when the marker and its manifest still match, ``"<cache>: prefetched"`` (with
    ``(was incomplete: ...)`` when it repaired a cache), ``"<cache>: incomplete
    (...)"`` when an incomplete cache cannot be prefetched again, ``"skipped (no
    host <tool>)"`` when the toolchain is not on ``PATH``, ``"skipped (offline)"``
    under an offline policy, or ``"failed (<reason>)"``; the build then fetches
    its dependencies online. *force* prefetches even a complete cache; *clear*
    first removes the cache and every marker of it (``fetch --force``).
    """
    marker = build.deps_marker(pin)
    if marker is None or not isinstance(build.build, LanguageBuild):
        return None
    recipe = build.build
    deps = root / DEPS_DIRNAME
    problem = deps_problem(build, pin, root)
    if problem is None and not force:
        return f"{recipe.deps}: kept"
    incomplete = problem not in (None, NOT_PREFETCHED)
    if offline or shutil.which(recipe.tool) is None:
        if problem is None:
            return f"{recipe.deps}: kept"
        if incomplete:
            return f"{recipe.deps}: incomplete ({problem})"
        return "skipped (offline)" if offline else f"skipped (no host {recipe.tool})"
    cache = deps / recipe.deps
    if clear:
        shutil.rmtree(cache, ignore_errors=True)
        for stale in deps.glob(f"*.{recipe.deps}.json"):
            stale.unlink()
    cache.mkdir(parents=True, exist_ok=True)
    argv, env = recipe.prefetch(str(cache.resolve()))
    scratch = Path(tempfile.mkdtemp(prefix=f".{build.pin_dir(pin)}.", dir=deps))
    try:
        tree = scratch / "tree"
        shutil.copytree(
            checkout, tree, symlinks=True, ignore=shutil.ignore_patterns(".git", FETCH_MARKER)
        )
        workdir = tree
        if isinstance(build.source, GitSource) and build.source.subdir:
            workdir = tree / build.source.subdir.strip("/")
        try:
            completed = run_tool(argv, cwd=workdir, env={**os.environ, **env})
        except OSError as exc:
            return f"failed (cannot run {recipe.tool}: {exc.strerror or exc})"
        contents = _deps_contents(recipe, workdir, tree, cache)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if completed.returncode != 0:
        lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
        last = lines[-1] if lines else f"exit status {completed.returncode}"
        return f"failed ({' '.join(argv[:2])}: {last})"
    record = {
        "build": build.key,
        "checkout": build.pin_dir(pin),
        "spec": build.deps_spec(),
        "contents": contents,
    }
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    (deps / marker).write_text(text, encoding="utf-8")
    if incomplete:
        return f"{recipe.deps}: prefetched (was incomplete: {problem})"
    return f"{recipe.deps}: prefetched"


def _git(source: GitSource, *args: str) -> None:
    described = source.describe()
    command = ["git", *args]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError as exc:
        reason = f"cannot run git: {exc.strerror or exc}"
        raise SourceError(
            f"Cannot fetch {described}: {reason}.",
            source=described,
            reason=reason,
            hint="Install git on the host that runs `tundravm fetch`.",
        ) from exc
    if completed.returncode != 0:
        reason = _git_error(completed)
        raise SourceError(
            f"Cannot fetch {described}: {reason}.",
            source=described,
            reason=reason,
            hint=(
                "Check the repository URL and that git on this host can read it "
                "(credentials, network), then run `tundravm fetch RECIPE` again."
            ),
            context={"argv": " ".join(command)},
        )


def _download(source: HttpSource, pin: str, dest: Path) -> None:
    described = source.describe()
    file = dest / source.filename
    digest = hashlib.sha256()
    try:
        with urlopen(source.url, timeout=_NETWORK_TIMEOUT) as response, file.open("wb") as out:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
    except HTTPError as exc:
        exc.close()
        reason = f"HTTP {exc.code}"
        raise SourceError(
            f"Cannot fetch {described}: {reason}.",
            source=described,
            reason=reason,
            hint="Check the URL with `curl -I URL`; it must serve the file.",
        ) from exc
    except OSError as exc:
        reason = _network_reason(exc.reason if isinstance(exc, URLError) else exc)
        raise SourceError(
            f"Cannot fetch {described}: {reason}.",
            source=described,
            reason=reason,
            hint="Check the URL and the network path to its host.",
        ) from exc
    if digest.hexdigest() != pin:
        reason = f"sha256 {digest.hexdigest()[:12]} does not match the pin {pin[:12]}"
        raise SourceError(
            f"Cannot fetch {described}: {reason}.",
            source=described,
            reason=reason,
            hint="The file changed upstream: `tundravm lock RECIPE --update NAME` pins it again.",
        )
    if source.filename.endswith(_ARCHIVE_SUFFIXES):
        unpack = ["tar", "-xf", str(file), "-C", str(dest), "--strip-components=1"]
        completed = subprocess.run(unpack, check=False, text=True, capture_output=True)
        if completed.returncode != 0:
            lines = completed.stderr.strip().splitlines()
            reason = f"cannot unpack {source.filename}: {lines[-1] if lines else 'tar failed'}"
            raise SourceError(
                f"Cannot fetch {described}: {reason}.",
                source=described,
                reason=reason,
                hint="Check that the archive is a tarball the host's tar can read.",
            )


def default_resolver(source: Source) -> str:
    """Resolve *source* over the network: ``git ls-remote`` or the sha256 of a download.

    Raises :class:`~tundravm.errors.SourceError` whose ``reason`` says why:
    ``ref '<ref>' not found``, ``repository unreachable: <git's error>``,
    ``HTTP <status>``, ``timed out after 60s``, or the network error. git never
    prompts for credentials: a private or missing repository is unreachable.
    """
    if source.inline_pin is not None:
        return source.inline_pin
    if isinstance(source, GitSource):
        return _ls_remote(source)
    return _download_digest(source)


def _ls_remote(source: GitSource) -> str:
    """The commit *source*'s ref points at, without prompting for credentials."""
    command = ["git", "ls-remote", source.url, source.ref]
    described = source.describe()
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=_NETWORK_TIMEOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        reason = f"timed out after {_NETWORK_TIMEOUT:g}s"
        raise SourceError(
            f"Cannot resolve {described}: {reason}.",
            source=described,
            reason=reason,
            hint="Check the network path to the repository, then lock again.",
        ) from None
    except OSError as exc:
        reason = f"cannot run git: {exc.strerror or exc}"
        raise SourceError(
            f"Cannot resolve {described}: {reason}.",
            source=described,
            reason=reason,
            hint="Install git on the host that runs `tundravm lock`.",
        ) from exc
    if completed.returncode != 0:
        reason = f"repository unreachable: {_git_error(completed)}"
        raise SourceError(
            f"Cannot resolve {described}: {reason}.",
            source=described,
            reason=reason,
            hint="Check the repository URL, its access rights and the network.",
            context={"argv": " ".join(command)},
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        reason = f"ref {source.ref!r} not found"
        raise SourceError(
            f"Cannot resolve {described}: {reason}.",
            source=described,
            reason=reason,
            hint=f"List the refs upstream has with `git ls-remote {source.url}`.",
        )
    return lines[0].split()[0]


def _git_error(completed: subprocess.CompletedProcess[str]) -> str:
    """The first ``fatal:`` line of git's stderr, else its first line or the exit code."""
    lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
    fatal = [line for line in lines if line.startswith("fatal:")]
    return (fatal or lines or [f"git exited {completed.returncode}"])[0]


def _download_digest(source: HttpSource) -> str:
    """The sha256 of the file at *source*'s url."""
    described = source.describe()
    try:
        with urlopen(source.url, timeout=_NETWORK_TIMEOUT) as response:
            return hashlib.sha256(response.read()).hexdigest()
    except HTTPError as exc:
        exc.close()
        reason = f"HTTP {exc.code}"
        raise SourceError(
            f"Cannot resolve {described}: {reason}.",
            source=described,
            reason=reason,
            hint="Check the URL in a browser or with `curl -I URL`; it must serve the file.",
        ) from exc
    except OSError as exc:
        reason = _network_reason(exc.reason if isinstance(exc, URLError) else exc)
        raise SourceError(
            f"Cannot resolve {described}: {reason}.",
            source=described,
            reason=reason,
            hint="Check the URL and the network path to its host.",
        ) from exc


def _network_reason(cause: object) -> str:
    if isinstance(cause, TimeoutError):
        return f"timed out after {_NETWORK_TIMEOUT:g}s"
    return str(cause)


def resolve_pins(
    builds: Mapping[str, NamedSource],
    previous: Mapping[str, LockedFetch],
    *,
    resolver: Resolver | None = None,
    offline: bool = False,
    kept: Sequence[LockedFetch] = (),
) -> list[LockedFetch]:
    """Lockfile entries for *builds*, after the *kept* pins of sources not in *builds*.

    Online, every mutable source is resolved again through *resolver* (default:
    :func:`default_resolver`). Offline, inline pins and matching *previous*
    entries are reused. Every source is attempted; when any fails, one
    :class:`LockfileError` lists each failed source with its reason and counts
    the *kept* pins as resolved; its ``failures`` maps the failed names to
    :class:`~tundravm.errors.SourceError`.
    """
    resolve = resolver or default_resolver
    pins: list[LockedFetch] = list(kept)
    failures: dict[str, SourceError] = {}
    for name, build in sorted(builds.items()):
        if build.source.inline_pin is not None:
            pins.append(build.locked(build.source.inline_pin))
            continue
        if offline:
            pin = build.pin_from(previous)
            if pin is None:
                failures[name] = _failure(build.source, "not pinned in the lockfile")
            else:
                pins.append(build.locked(pin))
            continue
        try:
            pins.append(build.locked(resolve(build.source)))
        except SourceError as exc:
            failures[name] = exc
        except (TdxError, OSError) as exc:
            failures[name] = _failure(build.source, _reason(exc))
    if failures:
        raise _unresolved(failures, len(kept) + len(builds), offline=offline)
    return pins


def _reason(exc: TdxError | OSError) -> str:
    """The message of a resolver error that is not a SourceError, without hint or context."""
    if isinstance(exc, TdxError) and exc.args:
        return str(exc.args[0])
    return str(exc) or type(exc).__name__


def _failure(source: Source, reason: str) -> SourceError:
    described = source.describe()
    return SourceError(
        f"Cannot resolve {described}: {reason}.",
        source=described,
        reason=reason,
        hint="Fix the source declaration or its upstream, then lock again.",
    )


def _unresolved(failures: Mapping[str, SourceError], total: int, *, offline: bool) -> LockfileError:
    """One error naming every source in *failures*, out of *total* to resolve."""
    count = len(failures)
    noun = "source" if count == 1 else "sources"
    if offline:
        verb = "needs" if count == 1 else "need"
        head = f"Cannot lock offline: {count} {noun} {verb} the network to resolve:"
        hint = (
            "Run `tundravm lock RECIPE` without --offline, or pin the source inline "
            "(Git(url, ref) with a 40-hex commit ref, or Http(url, sha256=...))."
        )
    else:
        head = f"Cannot lock: {count} {noun} could not be resolved:"
        hint = (
            "Fix the refs above, or drop NAME from --update to keep its existing pin, "
            "or pass --offline to reuse existing pins."
        )
    lines = [head]
    lines.extend(f"  {name}: {error.source}: {error.reason}" for name, error in failures.items())
    lines.append(f"{total - count} of {total} sources resolved; nothing written.")
    return LockfileError("\n".join(lines), hint=hint, failures=failures)


def source_drift(
    builds: Mapping[str, NamedSource],
    fetches: Mapping[str, LockedFetch],
    *,
    resolver: Resolver | None = None,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    """``(added, changed, removed, details)`` for the ``sources.<name>`` drift lines.

    *builds* and *fetches* are keyed by lock key; a per-variant key
    ``<variant>/<name>`` drifts as ``sources.<variant>.<name>`` (:func:`source_section`).

    A declared source without a lockfile entry is added, detailed as
    ``source <name> is not pinned`` unless the declaration pins it inline; one
    whose entry was resolved from a different url/ref, or (with *resolver*) whose
    ref now resolves elsewhere, is changed, detailed as ``<old sha7> -> <new sha7>``.
    Without *resolver* nothing touches the network.
    """
    added: list[str] = []
    changed: list[str] = []
    details: dict[str, str] = {}
    for name, build in sorted(builds.items()):
        section = source_section(name)
        locked = fetches.get(name)
        if locked is None:
            added.append(section)
            if build.source.inline_pin is None:
                details[section] = f"source {name} is not pinned"
            continue
        if build.matches(locked) and resolver is None:
            continue
        current = build.source.inline_pin
        if current is None and resolver is not None:
            current = resolver(build.source)
        if current == locked.digest:
            continue
        changed.append(section)
        new = current[:7] if current else build.source.requested
        details[section] = f"{locked.digest[:7]} -> {new}"
    removed = sorted(source_section(name) for name in fetches if name not in builds)
    return added, changed, removed, details


def source_section(key: str) -> str:
    """The drift section of the lockfile source *key*: ``sources.<name>``, or
    ``sources.<variant>.<name>`` for a variant-local ``<variant>/<name>``."""
    return "sources." + key.replace("/", ".")


__all__ = [
    "BuildRecipe",
    "CargoBuild",
    "DebFile",
    "DotnetBuild",
    "GitSource",
    "GoBuild",
    "HttpSource",
    "Install",
    "InstallKind",
    "KernelSource",
    "LanguageBuild",
    "NOT_PREFETCHED",
    "NamedSource",
    "Resolver",
    "ScriptBuild",
    "Source",
    "SourceBuild",
    "checkout_identity",
    "default_resolver",
    "deps_prefetched",
    "deps_problem",
    "fetch_source",
    "is_fetched",
    "prefetch_deps",
    "resolve_pins",
    "source_drift",
    "source_section",
    "write_marker",
]
