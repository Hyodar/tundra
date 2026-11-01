"""Source builds: a fetched source plus a build recipe, pinned through the lockfile.

A source is a git repository (:class:`GitSource`), a download (:class:`HttpSource`) or a
directory of the project (:class:`LocalSource`), pinned by commit, sha256 or content
digest (:func:`local_digest`).

``declarative.lower`` turns each declarative ``Build`` into a :class:`SourceBuild`
recorded on its profile's state; the internal modules declare theirs directly.

``install=`` lists what lands in the image, in order: :meth:`Install.artifact` is
the recipe's own output, :meth:`Install.file` and :meth:`Install.tree` copy further
paths of the source tree.

``tundravm.lock()`` resolves each source to a pin (``LockedFetch`` entries in
``tundravm.lock``) and the cache key carries it. The hook is *mounted*:
:func:`fetch_source` checks the pinned source out on the host as the invoking user,
into ``<out>/.sources/<name>-<pin[:12]>``, the backend mounts that directory at
``$SRCDIR/tundravm-sources`` and the hook copies the checkout into the build tree, so
the sandbox never fetches a source. A built kernel's source joins them as a
:class:`KernelSource` named ``kernel``: locked, fetched and mounted the same way.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .build_cache import Build, Cache, CacheDecl, CacheDir, CacheFile
from .errors import LockfileError, SourceError, StateError, TdxError, ValidationError
from .lockfile.model import LockedFetch

if TYPE_CHECKING:
    from .models import Kernel

COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PLAIN_ENV_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]*$")
_ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar.zst")
SOURCES_DIRNAME = ".sources"
"""Directory of the build output that ``tundravm build`` fetches the checkouts into."""
SOURCES_MOUNT = "tundravm-sources"
"""Where a mounted hook finds the host-fetched checkouts: ``$SRCDIR/tundravm-sources``."""
FETCH_MARKER = ".tundravm-complete"
"""File :func:`fetch_source`'s caller writes, holding the pin, once a checkout is complete."""
MOUNTED_CACHE_ROOT = "${BUILDDIR:-$BUILDROOT/build}"
"""The build cache of a mounted hook: mkosi's ``$BUILDDIR`` when ``BuildDirectory=`` is
configured (kept across builds), else the build overlay (discarded after the build)."""
DEPS_DIRNAME = "deps"
"""``<out>/.sources/deps``: the dependency caches ``tundravm build`` fills with the host's
toolchains (``go``, ``cargo``, ``nuget``), mounted with the checkouts."""
DEPS_BUILD_DIR = ".tundravm-deps"
"""Where a mounted hook copies a dependency cache: ``$BUILDROOT/build/.tundravm-deps/<cache>``
(``/build/.tundravm-deps/<cache>`` inside mkosi-chroot), so the mounted tree stays read-only."""
BUILD_MARKER = "# tundravm build: "
"""The first line of a mounted build hook: its declaration as JSON follows
(:meth:`SourceBuild.marker`)."""
_NETWORK_TIMEOUT = 60.0
"""Seconds :func:`default_resolver` waits on ``git ls-remote`` or a stalled download."""
GO_TOOLCHAIN_MODULE = "golang.org/toolchain"
"""The module the Go proxy serves each Go release as, ``@v0.0.1-<go>.linux-amd64``."""
GO_TOOLCHAIN_LOCAL = "local"
"""``GOTOOLCHAIN`` of a source whose go.mod predates toolchain modules: Debian's ``go``."""
GO_MOD_TOOLCHAIN = "<go.mod>"
"""The ``GOTOOLCHAIN`` a prefetch marker records: the one the checkout's go.mod names."""
_GO_VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+)|(beta|rc)(\d+))?$")
_GO_FIRST_TOOLCHAIN = (1, 21, 0, 0)
"""Go 1.21's language version: the first release published as a toolchain module."""
RUST_TOOLCHAIN_DEBIAN = "debian"
"""The toolchain of a Cargo source that pins none: Debian's ``cargo`` and ``rustc``."""
RUST_TOOLCHAIN_FILE = "<rust-toolchain.toml>"
"""The ``RUSTUP_TOOLCHAIN`` a prefetch marker records: the one the checkout's file pins."""
RUST_HOST = "x86_64-unknown-linux-gnu"
"""The host triple of every Rust toolchain ``tundravm build`` installs: the build sandbox's."""
RUSTUP_DIRNAME = "rustup"
"""``<out>/.sources/deps/rustup``: the ``RUSTUP_HOME`` the host's ``rustup`` installs into."""
_RUST_TOOLCHAIN_FILES = ("rust-toolchain", "rust-toolchain.toml")
"""The files ``rustup`` reads a directory's toolchain from, the legacy name first, as it does."""
_RUST_CHANNEL = re.compile(r"^(?:\d+\.\d+\.\d+|(?:stable|beta|nightly)-\d{4}-\d{2}-\d{2})$")
"""A pinned channel: a release (``1.91.0``) or a dated channel (``nightly-2026-09-01``)."""
_RUST_PROFILES = ("minimal", "default", "complete")
_RUST_DEBIAN_REASON = (
    "the source has no rust-toolchain.toml or rust-toolchain, so Debian's cargo builds it"
)


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


@dataclass(frozen=True, slots=True)
class LocalSource:
    """A directory of the project: *path* as the image file names it, *directory* on the host.

    Its pin is the content digest of the directory (:func:`local_digest`), computed on
    the host and never over the network, so ``lock`` records it again each time and a
    change of the directory is drift (:data:`LOCAL_CHANGED`). The checkout is a copy of
    the files :func:`local_files` lists.
    """

    path: str
    directory: Path = field(kw_only=True, compare=False, repr=False)

    kind: Literal["local"] = field(default="local", init=False, repr=False)

    @property
    def url(self) -> str:
        """The path: what the lockfile records as the source."""
        return self.path

    @property
    def requested(self) -> str:
        return self.path

    @property
    def inline_pin(self) -> str | None:
        return None

    def describe(self) -> str:
        """``local <path>``, as lock errors name the source."""
        return f"local {self.path}"

    def to_payload(self) -> dict[str, object]:
        return {"kind": self.kind, "path": self.path}

    def digest(self) -> str:
        """:func:`local_digest` of :attr:`directory`."""
        return local_digest(self.directory, self.describe())


Source = GitSource | HttpSource | LocalSource
Resolver = Callable[[Source], str]
"""Maps a source to its pin: a commit sha for git, a sha256 for http (a local source is
never resolved: its pin is :meth:`LocalSource.digest`)."""

LOCAL_CHANGED = "local-changed"
"""The drift of a local source whose directory no longer has the digest the lock pins."""


def local_files(directory: Path) -> list[str]:
    """The files of *directory* a local source holds: relative POSIX paths, sorted.

    Inside a git work tree it is what ``git ls-files --cached --others
    --exclude-per-directory=.gitignore`` lists there: the tracked files and the untracked
    ones the repository's ``.gitignore`` files do not ignore (only those, not
    ``.git/info/exclude`` or the user's global excludes, which differ from host to host).
    Outside one, or without ``git``, every file under it. ``.git`` is never part of it,
    and neither is a deleted tracked file. A regular file or a symlink is listed; anything
    else (a fifo, a socket, a device) is refused with :class:`~tundravm.errors.SourceError`.
    """
    listed = _git_listing(directory)
    found: set[str] = set()
    for rel in _walk_local(directory, "") if listed is None else listed:
        path = directory / rel
        if not path.is_symlink() and path.is_dir():
            found.update(_walk_local(directory, rel))
        elif os.path.lexists(path):
            found.add(rel)
    for rel in found:
        mode = (directory / rel).lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise SourceError(
                f"The local source {directory} holds {rel}, which is not a file or a symlink.",
                source=f"local {directory}",
                reason=f"{rel} is not a regular file or a symlink",
                hint=f"Remove {rel}, or list it in a .gitignore of the directory.",
            )
    return sorted(found)


def _git_listing(directory: Path) -> list[str] | None:
    """What git lists in *directory* (see :func:`local_files`), ``None`` outside a work tree."""
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-per-directory=.gitignore",
                "--",
                ".",
            ],
            check=False,
            capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    entries = completed.stdout.decode("utf-8", "surrogateescape").split("\0")
    return [entry.rstrip("/") for entry in entries if entry]


def _walk_local(directory: Path, prefix: str) -> list[str]:
    """Every file and symlink under ``directory/prefix``, relative to *directory*, but ``.git``."""
    found: list[str] = []
    for current, dirnames, filenames in os.walk(directory / prefix):
        base = Path(current).relative_to(directory)
        links = [d for d in dirnames if (Path(current) / d).is_symlink()]
        dirnames[:] = sorted(d for d in dirnames if d != ".git" and d not in links)
        found.extend((base / name).as_posix() for name in (*filenames, *links) if name != ".git")
    return found


def local_manifest(directory: Path) -> dict[str, str]:
    """``{path: sha256[ x] | -> target}`` of :func:`local_files`: a file's sha256, `` x``
    when it is executable, and a symlink's target."""
    found: dict[str, str] = {}
    for rel in local_files(directory):
        path = directory / rel
        if path.is_symlink():
            found[rel] = f"-> {os.readlink(path)}"
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        found[rel] = digest + (" x" if path.stat().st_mode & 0o111 else "")
    return found


def manifest_digest(manifest: Mapping[str, str]) -> str:
    """The sha256 of *manifest* (:func:`local_manifest`) as canonical JSON."""
    canonical = json.dumps(dict(manifest), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def local_digest(directory: Path, described: str | None = None) -> str:
    """The pin of a local source: :func:`manifest_digest` of :func:`local_manifest`.

    It covers each file's path, its sha256 and whether it is executable, and each
    symlink's target, in path order; nothing else (owners, other mode bits, times), so
    the same files hash the same on every host. A directory that holds no file is
    refused with :class:`~tundravm.errors.SourceError` (*described* names the source).
    """
    manifest = local_manifest(directory)
    if not manifest:
        label = described or f"local {directory}"
        raise SourceError(
            f"The {label} source holds no files.",
            source=label,
            reason=f"{directory} holds no file a build could use",
            hint="Check the path, and that a .gitignore does not ignore every file in it.",
        )
    return manifest_digest(manifest)


def copy_local(source: LocalSource, pin: str, dest: Path) -> None:
    """Copy the files of *source* into *dest* and check the copy is *pin*.

    A file is written ``0755`` when executable and ``0644`` otherwise, a symlink as its
    target. A copy whose digest is not *pin* (the directory changed since the lock)
    raises :class:`~tundravm.errors.SourceError` naming ``tundravm lock``.
    """
    for rel in local_files(source.directory):
        origin = source.directory / rel
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_symlink():
            target.symlink_to(os.readlink(origin))
            continue
        shutil.copyfile(origin, target)
        target.chmod(0o755 if origin.stat().st_mode & 0o111 else 0o644)
    copied = manifest_digest(_manifest(dest))
    if copied != pin:
        described = source.describe()
        reason = f"its digest is {copied[:12]}, the lock pins {pin[:12]}"
        raise SourceError(
            f"The {described} source changed since the lock ({LOCAL_CHANGED}).",
            source=described,
            reason=reason,
            hint="Run `tundravm lock` to record it, review and commit the lockfile, then build.",
        )


def _assignment(key: str, value: str) -> str:
    if _PLAIN_ENV_VALUE.fullmatch(value):
        return f"{key}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'{key}="{escaped}"'


_SOURCE_DATE_EPOCH = 'SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"'
"""A reproducible recipe's epoch: the build's ``SOURCE_DATE_EPOCH``, else ``0``."""


def _env_assignments(env: Mapping[str, str]) -> str:
    return " ".join(_assignment(key, value) for key, value in env.items())


def _since(default: object) -> dict[str, object]:
    """Field metadata: omit the field from the recipe payload while it holds *default*.

    Recipes declared before the field existed keep their payload, so their
    lockfile section digests stay fresh.
    """
    return {"payload_default": default}


class FrozenMap[V](Mapping[str, V]):
    """An immutable copy of a ``str -> V`` mapping, in the caller's order.

    Build recipes store ``env`` and ``properties`` as one, and declarations their
    mapping fields (``Service.env``, ``Template.variables``...), so changing the dict
    passed at construction changes nothing; it compares equal to a dict with
    the same items and hashes like one ``frozenset`` of them. Payloads sort it.
    """

    __slots__ = ("_items", "_lookup")

    def __init__(self, items: Mapping[str, V] | Iterable[tuple[str, V]] = ()) -> None:
        pairs = tuple(items.items() if isinstance(items, Mapping) else items)
        self._items: tuple[tuple[str, V], ...] = pairs
        self._lookup: dict[str, V] = dict(pairs)

    def __getitem__(self, key: str) -> V:
        return self._lookup[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._lookup)

    def __len__(self) -> int:
        return len(self._lookup)

    def __hash__(self) -> int:
        return hash(frozenset(self._lookup.items()))

    def __repr__(self) -> str:
        return f"FrozenMap({self._lookup!r})"

    def __reduce__(self) -> tuple[type[FrozenMap[V]], tuple[tuple[tuple[str, V], ...]]]:
        return (FrozenMap, (self._items,))

    def __copy__(self) -> FrozenMap[V]:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> FrozenMap[V]:
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
    ``reproducible`` adds ``-buildvcs=false``, ``CGO_ENABLED=0`` (unless ``cgo``
    or ``env`` sets it) and ``SOURCE_DATE_EPOCH`` (the build's, else ``0``); ``None``
    (the default) is on. The build runs the Go release the source's go.mod names
    (:func:`go_toolchain`), which ``tundravm build`` fetches on the host with the modules;
    ``env`` cannot set ``GOTOOLCHAIN``.
    """

    output: str
    package: str = "./..."
    ldflags: str = "-s -w -buildid="
    tags: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    packages: tuple[str, ...] = ("golang",)
    output_dir: str = field(default="./build", metadata=_since("./build"))
    mkdir: bool = field(default=True, metadata=_since(True))
    reproducible: bool | None = field(default=None, metadata=_since(None))
    cgo: bool = field(default=False, metadata=_since(False))

    kind: Literal["go"] = field(default="go", init=False, repr=False)
    tool: ClassVar[str] = "go"
    """The host executable ``tundravm build`` prefetches the dependencies with."""
    deps: ClassVar[str] = "go"
    """The dependency cache, ``<out>/.sources/deps/go``: a ``GOMODCACHE``."""

    def __post_init__(self) -> None:
        _freeze_recipe(self, ("env",), ("tags", "packages"))
        if "GOTOOLCHAIN" in self.env:
            raise ValidationError(
                "Go(env=...) sets GOTOOLCHAIN: the toolchain is the one the source's go.mod names.",
                hint="Drop GOTOOLCHAIN; to build with another Go, build a revision whose go.mod "
                "names it (its toolchain line, else its go line).",
            )

    def prefetch(
        self, cache: str, toolchain: str = GO_MOD_TOOLCHAIN
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """``go mod download`` into the module cache *cache* with the Go *toolchain*.

        ``GOTOOLCHAIN=<toolchain>`` makes the host ``go``, whatever its version, switch to
        that release, which it downloads into *cache* when absent. ``-modcacherw`` keeps
        the cache removable without ``go clean -modcache``.
        """
        env = {
            **self.env,
            "GOMODCACHE": cache,
            "GOFLAGS": "-mod=mod -modcacherw",
            "GOTOOLCHAIN": toolchain,
        }
        return ("go", "mod", "download"), env

    @staticmethod
    def deps_env(cache: str) -> str:
        """The exports pointing the in-build ``go`` at the copied module cache *cache*.

        Without network the cache is the only proxy and the checksum database is off: the
        host's ``go`` verified everything in it against ``go.sum`` and the database, and a
        toolchain module is never in ``go.sum``.
        """
        return (
            f"export GOMODCACHE={cache} GOFLAGS=-mod=mod && "
            '{ [ "$WITH_NETWORK" != 0 ] || '
            f"export GOPROXY=file://{cache}/cache/download GOSUMDB=off; }}"
        )

    @staticmethod
    def toolchain_env(record: str) -> str:
        """The export selecting the toolchain the prefetch wrote to *record*.

        Without a record (no host ``go``), ``local``: Debian's ``go``, which refuses a
        go.mod newer than itself instead of downloading a toolchain.
        """
        return f'export GOTOOLCHAIN="$(cat {record} 2>/dev/null || echo {GO_TOOLCHAIN_LOCAL})"'

    @property
    def artifact(self) -> str:
        return posixpath.normpath(f"{self.output_dir}/{self.output}")

    def command(self, workdir: str) -> str:
        env = f"{_env_assignments(self.env)} " if self.env else ""
        tags = f" -tags {','.join(self.tags)}" if self.tags else ""
        mkdir = f"mkdir -p {self.output_dir} && " if self.mkdir else ""
        flags = " -buildvcs=false" if self.reproducible else ""
        if self.reproducible:
            cgo = "" if self.cgo or "CGO_ENABLED" in self.env else "CGO_ENABLED=0 "
            epoch = "" if "SOURCE_DATE_EPOCH" in self.env else f"{_SOURCE_DATE_EPOCH} "
            env = f"{epoch}{cgo}{env}"
        return (
            f"cd {workdir} && {mkdir}{env}go build -trimpath{flags}{tags} "
            f'-ldflags "{self.ldflags}" -o {self.output_dir}/{self.output} {self.package}'
        )


CARGO_RUSTFLAGS = (
    "-C",
    "target-cpu=generic",
    "-C",
    "link-arg=-Wl,--build-id=none",
    "-C",
    "symbol-mangling-version=v0",
)
"""The rustc flags a reproducible ``CargoBuild`` adds: flashbots-images'
``scripts/build_rust_package.sh`` set (no host CPU features, no linker build id,
stable v0 symbol names)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CargoBuild:
    """``cargo fetch && cargo build --frozen``; *bin* or *package* selects the target.

    ``auditable`` builds with ``cargo auditable`` (adding the ``cargo-auditable``
    package), which embeds the crate list in a ``.dep-v0`` section the SBOM ``tundravm
    build`` writes reads. ``reproducible`` adds :data:`CARGO_RUSTFLAGS` and the path remap to the
    project's ``build.rustflags`` (``--config``; prepended to an ``env`` ``RUSTFLAGS``,
    which would replace them), one codegen unit, no incremental build and
    ``SOURCE_DATE_EPOCH`` (the build's, else ``0``). ``None`` (the default) is on. The
    build runs the toolchain the source's ``rust-toolchain.toml`` pins
    (:func:`rust_toolchain`), which ``tundravm build`` installs on the host with ``rustup``;
    a source that pins none builds with Debian's ``cargo``. ``env`` cannot set
    ``RUSTUP_TOOLCHAIN``.
    """

    output: str
    bin: str | None = None
    package: str | None = None
    features: tuple[str, ...] = ()
    profile: str = "release"
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    packages: tuple[str, ...] = ("cargo",)
    auditable: bool | None = field(default=None, metadata=_since(None))
    reproducible: bool | None = field(default=None, metadata=_since(None))

    kind: Literal["cargo"] = field(default="cargo", init=False, repr=False)
    tool: ClassVar[str] = "cargo"
    """The host executable ``tundravm build`` prefetches the dependencies with."""
    deps: ClassVar[str] = "cargo"
    """The dependency cache, ``<out>/.sources/deps/cargo``: a ``CARGO_HOME`` registry cache."""

    def __post_init__(self) -> None:
        _freeze_recipe(self, ("env",), ("features", "packages"))
        if self.auditable is True and "cargo-auditable" not in self.packages:
            object.__setattr__(self, "packages", (*self.packages, "cargo-auditable"))
        if "RUSTUP_TOOLCHAIN" in self.env:
            raise ValidationError(
                "Cargo(env=...) sets RUSTUP_TOOLCHAIN: the toolchain is the one the source's "
                "rust-toolchain.toml pins.",
                hint="Drop RUSTUP_TOOLCHAIN; to build with another Rust, build a revision whose "
                'rust-toolchain.toml pins it (channel = "1.91.0").',
            )

    def prefetch(
        self, cache: str, toolchain: str = RUST_TOOLCHAIN_FILE
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """``cargo fetch --locked`` into the cargo home *cache* with the Rust *toolchain*.

        ``RUSTUP_TOOLCHAIN`` names the installed toolchain, whose own ``cargo``
        :func:`prefetch_deps` puts first on ``PATH``; with :data:`RUST_TOOLCHAIN_DEBIAN`
        it is unset and the host's ``cargo`` fetches.
        """
        env = {**self.env, "CARGO_HOME": cache}
        if toolchain != RUST_TOOLCHAIN_DEBIAN:
            env["RUSTUP_TOOLCHAIN"] = toolchain
        return ("cargo", "fetch", "--locked"), env

    @staticmethod
    def deps_env(cache: str) -> str:
        """The exports pointing the in-build ``cargo`` at the copied cargo home *cache*."""
        return (
            f"export CARGO_HOME={cache} && "
            '{ [ "$WITH_NETWORK" != 0 ] || export CARGO_NET_OFFLINE=true; }'
        )

    @staticmethod
    def toolchain_env(record: str, rustup: str) -> str:
        """The exports selecting the toolchain the prefetch wrote to *record*.

        Its own ``cargo`` and ``rustc`` come first on ``PATH``: Debian's ``cargo`` is not
        rustup's proxy, so ``RUSTUP_TOOLCHAIN`` alone would not select it. Without a
        record, or with ``debian``, Debian's ``cargo`` builds.
        """
        name = f'"$(cat {record})"'
        return (
            f"if [ -f {record} ] && [ {name} != {RUST_TOOLCHAIN_DEBIAN} ]; then "
            f"export RUSTUP_HOME={rustup} RUSTUP_TOOLCHAIN={name} && "
            'export PATH="$RUSTUP_HOME/toolchains/$RUSTUP_TOOLCHAIN/bin:$PATH"; fi'
        )

    @property
    def artifact(self) -> str:
        return f"target/{self.profile}/{self.bin or self.package or self.output}"

    def _environment(self, workdir: str) -> tuple[dict[str, str], str]:
        """The exports and the ``--config`` argument :attr:`reproducible` adds to ``env``."""
        if not self.reproducible:
            return dict(self.env), ""
        rustflags = [*CARGO_RUSTFLAGS, f"--remap-path-prefix={workdir}=/build"]
        prefix = f"CARGO_PROFILE_{self.profile.upper().replace('-', '_')}"
        env = {
            f"{prefix}_CODEGEN_UNITS": "1",
            f"{prefix}_INCREMENTAL": "false",
            "CARGO_TERM_COLOR": "never",
            **self.env,
        }
        if "RUSTFLAGS" in self.env:
            env["RUSTFLAGS"] = " ".join((*rustflags, self.env["RUSTFLAGS"]))
            return env, ""
        toml = "build.rustflags=" + json.dumps(rustflags, separators=(",", ":"))
        return env, f" --config {shlex.quote(toml)}"

    def command(self, workdir: str) -> str:
        environment, config = self._environment(workdir)
        exports = _env_assignments(environment)
        if self.reproducible and "SOURCE_DATE_EPOCH" not in environment:
            exports = f"{_SOURCE_DATE_EPOCH} {exports}"
        env = f"export {exports} && " if exports else ""
        profile = "--release" if self.profile == "release" else f"--profile {self.profile}"
        flags = config
        if self.features:
            flags += f" --features {','.join(self.features)}"
        if self.package:
            flags += f" --package {self.package}"
        if self.bin:
            flags += f" --bin {self.bin}"
        cargo = "cargo auditable" if self.auditable else "cargo"
        return f"{env}cd {workdir} && cargo fetch && {cargo} build {profile} --frozen{flags}"


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
    """The host executable ``tundravm build`` prefetches the dependencies with."""
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
"""The build recipes whose dependencies ``tundravm build`` prefetches on the host."""


def resolve_recipe(build: BuildRecipe) -> BuildRecipe:
    """*build* with its defaults decided: a ``None`` ``reproducible`` or ``auditable`` is on."""
    if isinstance(build, GoBuild) and build.reproducible is None:
        return replace(build, reproducible=True)
    if isinstance(build, CargoBuild) and None in (build.reproducible, build.auditable):
        return replace(
            build,
            reproducible=True if build.reproducible is None else build.reproducible,
            auditable=True if build.auditable is None else build.auditable,
        )
    return build


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


def _compact(payload: Mapping[str, object]) -> dict[str, object]:
    """*payload* without its ``None`` values."""
    return {key: value for key, value in payload.items() if value is not None}


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

    The kernel build script copies its host-fetched checkout from
    ``$SRCDIR/tundravm-sources/<name>-<pin[:12]>``.
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
class KernelFile(NamedSource):
    """A file a composed kernel's build reads: its Debian config package or an http patch.

    Named ``kernel-config`` or ``kernel-patch-<patch>`` (``-<variant>`` appended when
    the variant's differs from the default's), it is locked by sha256 and fetched on
    the host like any source; :meth:`mounted` checks the mounted copy in the build.
    """

    name: str
    source: HttpSource
    lock_name: str | None = field(default=None, compare=False, repr=False, kw_only=True)

    def mounted(self, pin: str | None) -> tuple[str, tuple[str, ...]]:
        """``(path, shell lines)``: the sha256-checked copy at *path*, else lines that fail."""
        effective = self.source.inline_pin or pin
        if effective is None:
            message = f"tundravm: {self.name} is not pinned: run tundravm lock, then tundravm build"
            return "", (f"echo {shlex.quote(message)} >&2", "exit 1")
        directory = f"$SRCDIR/{SOURCES_MOUNT}/{self.pin_dir(effective)}"
        path = f'"{directory}/{self.source.filename}"'
        missing = shlex.quote(
            f"tundravm: {self.pin_dir(effective)} is not fetched: run tundravm build"
        )
        return path, (
            f'[ -f "{directory}/{FETCH_MARKER}" ] || {{ echo {missing} >&2; exit 1; }}',
            f'echo "{effective}  {directory}/{self.source.filename}" | sha256sum -c --quiet -',
        )


@dataclass(frozen=True, slots=True)
class DebFile(NamedSource):
    """A ``.deb`` a postinst hook installs with ``dpkg -i``: :class:`EfiStub`'s package.

    It is a source like the others: ``lock`` pins its
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
        missing = f"tundravm: {self.pin_dir(effective)} is not fetched: run tundravm build"
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

    The hook copies the host-fetched checkout and caches under
    ``<namespace>-<fingerprint>``: the namespace is ``cache_key`` (default: the build
    name) and the fingerprint (:meth:`cache_fingerprint`) covers the pin, the build and
    install specs, the toolchain and the distribution it comes from, so changing any of
    them rebuilds.
    """

    name: str
    source: Source
    build: BuildRecipe
    install: tuple[Install, ...] = ()
    cache_key: str | None = None
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
        """Build packages the build recipe needs, its defaults decided (``cargo-auditable``)."""
        return self.build_packages()

    def build_packages(self) -> tuple[str, ...]:
        """:attr:`packages`: the recipe's own (the checkout is fetched on the host)."""
        return tuple(dict.fromkeys(resolve_recipe(self.build).packages))

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
        }

    def render(
        self,
        pin: str | None = None,
        *,
        distribution: Mapping[str, object] | None = None,
    ) -> str:
        """The build-phase hook: copy the checkout, build, cache, install.

        *pin* is a lockfile pin; an inline pin (commit ref, ``sha256=``) wins. The hook
        copies the host-fetched checkout ``$SRCDIR/tundravm-sources/<name>-<pin[:12]>``;
        unpinned, it only fails, naming ``tundravm lock`` and ``tundravm build``.
        *distribution* (the variant's ``Lowered.build_distribution``) enters the cache
        fingerprint.
        """
        return f"{self.marker()}\n{self._render(pin, distribution)}"

    def marker(self) -> str:
        """``# tundravm build: <json>``: the declaration a mounted hook is rendered from.

        The JSON is :meth:`to_payload` without ``None`` values, keys sorted and no
        spaces: what ``tundravm import`` reads the build back from. It holds no lockfile
        pin, so a pinned hook names the same.
        """
        payload = _compact(
            {
                "name": self.name,
                "source": _compact(self.source.to_payload()),
                "build": _recipe_payload(self.build),
                "install": [
                    _compact({"kind": s.kind, "path": s.path, "dest": s.dest, "mode": s.mode})
                    for s in self.install
                ],
                "cache_key": self.cache_key,
            }
        )
        return BUILD_MARKER + json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _render(self, pin: str | None, distribution: Mapping[str, object] | None) -> str:
        effective = self.source.inline_pin or pin
        workdir = Build.chroot_path(self.name).rel
        if isinstance(self.source, GitSource) and self.source.subdir:
            workdir = f"{workdir}/{self.source.subdir.strip('/')}"
        build = resolve_recipe(self.build).command(f"/build/{workdir}")
        if effective is None:
            message = (
                f"tundravm: source build {self.name} is not pinned: "
                "run tundravm lock, then tundravm build"
            )
            unpinned = f"# unpinned: {self.source.requested}\n"
            return f"{unpinned}echo {shlex.quote(message)} >&2 && exit 1"
        copy_deps, uses_deps = self._deps(effective)
        inner = f"{uses_deps}{build}".replace("'", "'\\''")
        command = f"{self._copy(effective)}{copy_deps} && mkosi-chroot bash -c '{inner}'"
        fingerprint = self.cache_fingerprint(effective, distribution=distribution or {})
        key = f"{self.cache_key or self.name}-{fingerprint}"
        return self._cache(key, workdir).wrap(command, root=MOUNTED_CACHE_ROOT)

    def _copy(self, pin: str) -> str:
        """Copy the mounted checkout of *pin* to the build tree, without its marker."""
        source = f'"$SRCDIR/{SOURCES_MOUNT}/{self.pin_dir(pin)}"'
        target = f'"{Build.build_path(self.name)}"'
        missing = f"tundravm: {self.pin_dir(pin)} is not fetched: run tundravm build"
        return (
            f"[ -f {source}/{FETCH_MARKER} ] || {{ echo {shlex.quote(missing)} >&2; exit 1; }}"
            f" && mkdir -p {target} && cp -a --no-preserve=ownership {source}/. {target}/"
            f" && rm -f {target}/{FETCH_MARKER}"
        )

    def _deps(self, pin: str) -> tuple[str, str]:
        """``(copy, exports)``: a mounted hook's use of the prefetched dependency cache.

        *copy* (``&&``-joined after the checkout copy) copies
        ``$SRCDIR/tundravm-sources/deps/<cache>`` to ``$BUILDROOT/build/.tundravm-deps``
        once, when ``tundravm build`` filled it, and a Go or Cargo build's toolchain record
        (:meth:`toolchain_file`) beside it, with the Rust toolchain it names from
        ``deps/rustup``; *exports* (inside mkosi-chroot) point the toolchain at that copy,
        offline when mkosi runs the build without network, and select the build's
        toolchain. Both are empty for a script build.
        """
        if not isinstance(self.build, LanguageBuild):
            return "", ""
        cache = self.build.deps
        mounted_deps = f"$SRCDIR/{SOURCES_MOUNT}/{DEPS_DIRNAME}"
        mounted = f'"{mounted_deps}/{cache}"'
        parent = f'"{Build.build_path(DEPS_BUILD_DIR)}"'
        target = f'"{Build.build_path(f"{DEPS_BUILD_DIR}/{cache}")}"'
        copy = (
            f" && {{ [ ! -d {mounted} ] || [ -d {target} ] || "
            f"{{ mkdir -p {parent} && cp -a --no-preserve=ownership {mounted} {target}; }}; }}"
        )
        chroot = Build.chroot_path(f"{DEPS_BUILD_DIR}/{cache}")
        exports = f"if [ -d {chroot} ]; then {self.build.deps_env(str(chroot))}; fi && "
        record = self.toolchain_file(pin)
        if record is None:
            return copy, exports
        mounted_record = f'"{mounted_deps}/{record}"'
        copy += (
            f" && {{ [ ! -f {mounted_record} ] || "
            f"{{ mkdir -p {parent} && cp {mounted_record} {parent}/; }}; }}"
        )
        in_chroot = Build.chroot_path(f"{DEPS_BUILD_DIR}/{record}")
        if isinstance(self.build, GoBuild):
            return copy, exports + f"{GoBuild.toolchain_env(str(in_chroot))} && "
        name = f'"$(cat {mounted_record})"'
        toolchains = f"{RUSTUP_DIRNAME}/toolchains"
        copied = f'"{Build.build_path(f"{DEPS_BUILD_DIR}/{toolchains}")}"'
        copy += (
            f" && {{ [ ! -f {mounted_record} ] || [ {name} = {RUST_TOOLCHAIN_DEBIAN} ] || "
            f"[ -d {copied}/{name} ] || {{ mkdir -p {copied} && "
            f'cp -a --no-preserve=ownership "{mounted_deps}/{toolchains}"/{name} {copied}/; }}; }}'
        )
        rustup = Build.chroot_path(f"{DEPS_BUILD_DIR}/{RUSTUP_DIRNAME}")
        return copy, exports + f"{CargoBuild.toolchain_env(str(in_chroot), str(rustup))} && "

    def toolchain_file(self, pin: str) -> str | None:
        """``<pin_dir>.<cache>.toolchain``: the toolchain a Go or Cargo prefetch selected.

        ``tundravm build`` writes it in ``.sources/deps`` once the host fetched that
        toolchain: a Go build's ``GOTOOLCHAIN`` (:func:`go_toolchain`), a Cargo build's
        installed Rust toolchain or ``debian`` (:func:`rust_toolchain`). ``None`` for a
        build that is neither.
        """
        if not isinstance(self.build, (GoBuild, CargoBuild)):
            return None
        return f"{self.pin_dir(pin)}.{self.build.deps}.toolchain"

    def deps_marker(self, pin: str) -> str | None:
        """``<pin_dir>.<cache>.json``: the file in ``.sources/deps`` that records a prefetch.

        ``tundravm build`` writes it once the host toolchain filled the cache for
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
        ``submodules``), the build recipe (defaults decided, see
        :func:`resolve_recipe`) and install steps, the toolchain (recipe kind and packages, and
        that Go's go.mod or Rust's rust-toolchain.toml picks the release) and the
        *distribution* it comes from: base, architecture, mirror, snapshot,
        repositories, build packages and build settings
        (``Lowered.build_distribution``).
        """
        source: dict[str, object] = {"kind": self.source.kind, "pin": pin}
        if isinstance(self.source, GitSource):
            source.update(subdir=self.source.subdir, submodules=self.source.submodules)
        spec = {
            "source": source,
            "build": _recipe_payload(resolve_recipe(self.build)),
            "install": [
                {"kind": step.kind, "path": step.path, "dest": step.dest, "mode": step.mode}
                for step in self.install
            ],
            "distribution": dict(distribution),
            "toolchain": {
                "kind": self.build.kind,
                "packages": list(self.build_packages()),
                **_TOOLCHAIN_VERSIONS.get(self.build.kind, {}),
            },
        }
        return fingerprint(spec)

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


_TOOLCHAIN_VERSIONS: dict[str, dict[str, str]] = {
    "go": {"version": GO_MOD_TOOLCHAIN},
    "cargo": {"version": RUST_TOOLCHAIN_FILE},
}
"""What :meth:`SourceBuild.cache_fingerprint` records picks a recipe kind's toolchain."""


def fingerprint(spec: Mapping[str, object]) -> str:
    """The first 16 hex of the sha256 over the canonical JSON of *spec*.

    :meth:`SourceBuild.cache_fingerprint` hashes a build this way, and the
    kernel cache key a variant's ``Lowered.build_distribution``.
    """
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


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
    """Mark *checkout* complete: its pin, its source identity and, for http and local, a
    file manifest.

    :func:`is_fetched` verifies the checkout against it before reuse.
    """
    marker: dict[str, object] = {"pin": pin, "source": _identity_payload(source)}
    if isinstance(source, (HttpSource, LocalSource)):
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
        f"The checkout of source {label} is modified or incomplete.",
        source=source.describe(),
        reason=problem,
        hint=(
            f"{problem}. Run `tundravm build --clean-cache` to check {label} out again "
            "(it drops the image's workspace, sources included)."
        ),
        context={"path": str(checkout)},
    )


def _checkout_problem(
    checkout: Path, source: Source, pin: str, marker: Mapping[str, object]
) -> str | None:
    """Why *checkout* no longer holds *source* at *pin*, or ``None`` when it does."""
    if marker.get("source") != _identity_payload(source):
        return "the checkout was fetched for another source declaration"
    if isinstance(source, (HttpSource, LocalSource)):
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
    when it is a tarball, as the in-sandbox hook did. local: a copy of its files
    (:func:`copy_local`), which must still be *pin*. Raises
    :class:`~tundravm.errors.SourceError` naming the source and the reason.
    """
    dest.mkdir(parents=True)
    if isinstance(source, LocalSource):
        copy_local(source, pin, dest)
        return
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
    restore's ``project.assets.json``, and the toolchain record naming the recorded
    toolchain, a Rust one with its files in ``deps/rustup``. ``None`` for a script
    build, which has none; :data:`NOT_PREFETCHED` without a marker.
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
    record = build.toolchain_file(pin)
    if record is None:
        return None
    toolchain = recorded.get("toolchain")
    named = toolchain.get("name") if isinstance(toolchain, dict) else None
    try:
        written = (root / DEPS_DIRNAME / record).read_text(encoding="utf-8").strip()
    except OSError:
        written = None
    language = "Go" if isinstance(build.build, GoBuild) else "Rust"
    if named is None or written != named:
        return f"{DEPS_DIRNAME}/{record} does not name the recorded {language} toolchain"
    files = recorded.get("toolchain_contents")
    if isinstance(build.build, CargoBuild) and named != RUST_TOOLCHAIN_DEBIAN:
        home = root / DEPS_DIRNAME / RUSTUP_DIRNAME
        for rel, expected in sorted(files.items() if isinstance(files, dict) else ()):
            if _cache_entry(home / rel) != expected:
                return f"{DEPS_DIRNAME}/{RUSTUP_DIRNAME}/{rel} is missing or changed"
        if not files:
            return "the marker has no content manifest of the Rust toolchain"
    return None


def toolchain_record(build: SourceBuild, pin: str, root: Path) -> dict[str, object] | None:
    """The Go or Rust toolchain the prefetch of *build* at *pin* recorded in ``<root>/deps``.

    Go: ``{"name", "module", "sum"}``, the ``GOTOOLCHAIN`` release, its module and the
    ``h1:`` checksum the checksum database vouched for, or ``{"name": "local", "reason"}``
    for a go.mod older than toolchain modules. Rust: :meth:`RustToolchain.record` with
    what ``rustc -vV`` says, or ``{"name": "debian", "reason"}`` for a source that pins
    none. ``None`` for another build or one that no prefetch recorded.
    """
    marker = build.deps_marker(pin)
    if marker is None or build.toolchain_file(pin) is None:
        return None
    try:
        recorded = json.loads((root / DEPS_DIRNAME / marker).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    toolchain = recorded.get("toolchain") if isinstance(recorded, dict) else None
    return dict(toolchain) if isinstance(toolchain, dict) else None


def _go_mod_lookup(text: str, key: str) -> str | None:
    """The value of go.mod's first *key* line, comment stripped (``gover.GoModLookup``)."""
    for line in text.splitlines():
        words = line.strip().split(None, 1)
        if len(words) == 2 and words[0] == key:
            return words[1].split("//", 1)[0].strip()
    return None


def _go_version_key(version: str) -> tuple[int, int, int, int] | None:
    """A Go version's sort key: ``1.21`` < ``1.21beta1`` < ``1.21rc1`` < ``1.21.0``."""
    match = _GO_VERSION.fullmatch(version)
    if match is None:
        return None
    major, minor, patch, kind, number = match.groups()
    if patch is not None:
        return int(major), int(minor), 3, int(patch)
    if kind is not None:
        return int(major), int(minor), 1 if kind == "beta" else 2, int(number)
    return int(major), int(minor), 0, 0


def go_toolchain(go_mod: str) -> str:
    """The ``GOTOOLCHAIN`` the go.mod (or go.work) text *go_mod* selects.

    The newer of its ``toolchain`` line and its ``go`` line, as Go's own selection reads
    them: ``go 1.N`` for N ≥ 21 is a language version, built by its first release
    ``go1.N.0``; ``toolchain default`` names nothing. ``local`` when that is older than Go
    1.21, which publishes no toolchain module (no ``go`` line means ``go 1.16``): Debian's
    ``go`` builds it. A version that is not a Go release raises ``SourceError``.
    """
    go = _go_mod_lookup(go_mod, "go") or "1.16"
    best = _go_version_key(go)
    if best is None:
        raise _not_a_release(f"go {go}")
    name = f"go{go}.0" if best[2] == 0 and best >= _GO_FIRST_TOOLCHAIN else f"go{go}"
    line = _go_mod_lookup(go_mod, "toolchain")
    if line is not None and line != "default":
        key = _go_version_key(line.removeprefix("go")) if line.startswith("go") else None
        if key is None:
            raise _not_a_release(f"toolchain {line}")
        if key > best:
            best, name = key, line
    return name if best >= _GO_FIRST_TOOLCHAIN else GO_TOOLCHAIN_LOCAL


def _not_a_release(line: str) -> SourceError:
    return SourceError(
        f"go.mod's `{line}` is not a Go release, so no toolchain can be fetched for it.",
        source="go.mod",
        reason=f"`{line}` is not a Go release",
        hint="Build a revision whose go.mod names a Go release (go1.N.P, go1.NrcM).",
    )


def _go_mod_file(workdir: Path, tree: Path) -> Path | None:
    """The go.work, else the go.mod, ``go`` in *workdir* reads its version lines from.

    Each is looked for in *workdir* and its parents up to *tree*, as ``go`` looks.
    """
    for name in ("go.work", "go.mod"):
        for directory in _up_to(workdir, tree):
            if (directory / name).is_file():
                return directory / name
    return None


def _up_to(workdir: Path, tree: Path) -> list[Path]:
    """*workdir* and its parents up to the source tree *tree*, nearest first."""
    return [workdir, *(d for d in workdir.parents if d == tree or tree in d.parents)]


def host_go_version() -> str | None:
    """The release of the host's ``go`` (``go1.24.4``), as ``go version`` names it unswitched."""
    path = shutil.which("go")
    if path is None:
        return None
    try:
        completed = subprocess.run(
            [path, "version"],
            env={**os.environ, "GOTOOLCHAIN": "local"},
            capture_output=True,
            text=True,
            timeout=_NETWORK_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    words = completed.stdout.split()
    return words[2] if completed.returncode == 0 and len(words) > 2 else None


def require_host_go(builds: Iterable[NamedSource]) -> None:
    """Refuse *builds* holding a Go build when the host has no ``go`` 1.21+ to prefetch it.

    Raised before anything is fetched, since the offline sandbox cannot build a Go source
    whose toolchain and modules the host did not fetch.
    """
    go = [b.key for b in builds if isinstance(b, SourceBuild) and isinstance(b.build, GoBuild)]
    if not go:
        return
    found = host_go_version()
    key = _go_version_key(found.removeprefix("go")) if found else None
    if key is not None and key >= _GO_FIRST_TOOLCHAIN:
        return
    names = ", ".join(repr(name) for name in go)
    noun = f"Go source build {names} needs" if len(go) == 1 else f"Go source builds {names} need"
    have = "there is no go on PATH" if found is None else f"the host's go is {found}"
    raise StateError(
        f"{noun} Go 1.21 or newer on the host to fetch its toolchain and modules: {have}.",
        hint=(
            "Install Go 1.21 or newer (the Debian package golang, or https://go.dev/dl), "
            "then check the host with `tundravm doctor --for build`."
        ),
        context={"sources": ", ".join(go), "go": found or "none"},
    )


def _go_toolchain_module(name: str) -> str:
    """``golang.org/toolchain@v0.0.1-<name>.linux-amd64``: the module of release *name*."""
    return f"{GO_TOOLCHAIN_MODULE}@v0.0.1-{name}.linux-amd64"


def _go_toolchain_entries(name: str, cache: Path) -> tuple[dict[str, object], dict[str, str]]:
    """``(record, contents)`` of release *name* in the module cache *cache*.

    The record is :func:`go_toolchain_record`'s; the contents are the manifest entries of
    the extracted toolchain and its ``.ziphash`` (empty when either is missing).
    """
    if name == GO_TOOLCHAIN_LOCAL:
        reason = "go.mod names no Go 1.21 or newer, the first with a toolchain module"
        return {"name": name, "reason": reason}, {}
    module = _go_toolchain_module(name)
    version = module.split("@", 1)[1]
    ziphash = f"cache/download/{GO_TOOLCHAIN_MODULE}/@v/{version}.ziphash"
    contents: dict[str, str] = {}
    for rel in (module, ziphash):
        entry = _cache_entry(cache / rel)
        if entry is not None:
            contents[rel] = entry
    try:
        checksum = (cache / ziphash).read_text(encoding="utf-8").strip()
    except OSError:
        checksum = ""
    if len(contents) != 2 or not checksum.startswith("h1:"):
        return {"name": name, "module": module}, {}
    return {"name": name, "module": module, "sum": checksum}, contents


def _fetch_go_toolchain(
    build: SourceBuild, name: str, workdir: Path, cache: Path, env: Mapping[str, str]
) -> tuple[dict[str, object], dict[str, str]]:
    """Download Go release *name* into the module cache *cache* with the host ``go``.

    ``go mod download golang.org/toolchain@...`` under ``GOTOOLCHAIN=<name>``: a host
    ``go`` of another version switches to *name* first, which downloads it; one of that
    version downloads the module itself. Either way the checksum database vouches for it.
    Returns :func:`_go_toolchain_entries`; a failure raises ``SourceError`` naming the
    release, its module and the fix.
    """
    module = _go_toolchain_module(name)
    argv = ("go", "mod", "download", module)
    try:
        completed = run_tool(argv, cwd=workdir, env={**os.environ, **env})
    except OSError as exc:
        reason = f"cannot run go: {exc.strerror or exc}"
    else:
        lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
        reason = lines[-1] if lines else f"exit status {completed.returncode}"
        if completed.returncode == 0:
            record, contents = _go_toolchain_entries(name, cache)
            if contents:
                return record, contents
            reason = f"{module} is not in the module cache after the download"
    raise SourceError(
        f"Cannot fetch {name}, the Go toolchain source build {build.key!r}'s go.mod names: "
        f"{reason}.",
        source=build.source.describe(),
        reason=reason,
        hint=(
            f"tundravm downloads it on the host as the module {module} from the Go module "
            "proxy (GOPROXY, proxy.golang.org by default), then builds offline with it. Give "
            "the host access to the proxy, or build a revision whose go.mod names a toolchain "
            "the proxy serves."
        ),
        context={"toolchain": name, "module": module},
    )


@dataclass(frozen=True, slots=True)
class RustToolchain:
    """The Rust toolchain a source pins: its *channel* and what ``rustup`` installs with it.

    *file* is the source's ``rust-toolchain.toml`` (or ``rust-toolchain``) it comes from;
    *profile* is ``minimal`` unless the file names another.
    """

    channel: str
    components: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    profile: str = "minimal"
    file: str = "rust-toolchain.toml"

    @property
    def name(self) -> str:
        """``<channel>-x86_64-unknown-linux-gnu``: its directory in ``RUSTUP_HOME/toolchains``."""
        return f"{self.channel}-{RUST_HOST}"

    def install(self) -> tuple[str, ...]:
        """The ``rustup toolchain install`` of this toolchain, never updating rustup."""
        argv = ["rustup", "toolchain", "install", self.name, "--profile", self.profile]
        argv.append("--no-self-update")
        if self.components:
            argv += ["--component", ",".join(self.components)]
        if self.targets:
            argv += ["--target", ",".join(self.targets)]
        return tuple(argv)


def rust_toolchain(workdir: Path, tree: Path) -> RustToolchain | None:
    """The Rust toolchain the source tree *tree* pins for a build in *workdir*.

    As ``rustup`` looks: the first directory from *workdir* up to *tree* holding a
    ``rust-toolchain`` (a channel line, or TOML) or a ``rust-toolchain.toml``, the legacy
    name first. ``None`` without one: Debian's ``cargo`` builds it. A channel that is not
    pinned (``stable``, ``1.91``, an undated ``nightly``), a ``path`` toolchain or a file
    that is not a toolchain file raises ``SourceError`` naming the fix.
    """
    for directory in _up_to(workdir, tree):
        for name in _RUST_TOOLCHAIN_FILES:
            path = directory / name
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                return _rust_toolchain_file(text, path.relative_to(tree).as_posix())
    return None


def checkout_rust_toolchain(build: SourceBuild, checkout: Path) -> RustToolchain | None:
    """:func:`rust_toolchain` of a Cargo *build*'s *checkout*; ``None`` for another build."""
    if not isinstance(build.build, CargoBuild):
        return None
    workdir = checkout
    if isinstance(build.source, GitSource) and build.source.subdir:
        workdir = checkout / build.source.subdir.strip("/")
    return rust_toolchain(workdir, checkout)


def _rust_toolchain_file(text: str, file: str) -> RustToolchain:
    try:
        table: object = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if posixpath.basename(file) != "rust-toolchain" or len(lines) != 1:
            raise _not_a_toolchain(file, "it is not TOML with a [toolchain] table") from None
        return _pinned(RustToolchain(lines[0], file=file))
    toolchain = table.get("toolchain") if isinstance(table, dict) else None
    if not isinstance(toolchain, dict):
        raise _not_a_toolchain(file, "it has no [toolchain] table")
    if "path" in toolchain:
        raise _not_a_toolchain(file, "it names a custom toolchain (path =), not a channel")
    channel = toolchain.get("channel")
    profile = toolchain.get("profile", "minimal")
    if not isinstance(channel, str):
        raise _not_a_toolchain(file, "its [toolchain] names no channel")
    if profile not in _RUST_PROFILES:
        raise _not_a_toolchain(file, f"profile {profile!r} is not minimal, default or complete")
    lists = {key: toolchain.get(key, []) for key in ("components", "targets")}
    for key, value in lists.items():
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise _not_a_toolchain(file, f"its {key} is not a list of names")
    return _pinned(
        RustToolchain(
            channel,
            components=tuple(lists["components"]),
            targets=tuple(lists["targets"]),
            profile=str(profile),
            file=file,
        )
    )


def _pinned(toolchain: RustToolchain) -> RustToolchain:
    if _RUST_CHANNEL.fullmatch(toolchain.channel):
        return toolchain
    reason = f"channel {toolchain.channel!r} is not pinned"
    raise SourceError(
        f"{toolchain.file} asks for Rust channel {toolchain.channel!r}, which is not pinned: "
        "it moves with each Rust release, so two builds would not build the same bytes.",
        source=toolchain.file,
        reason=reason,
        hint=(
            f'Pin a release in the source\'s {toolchain.file} (channel = "1.91.0", or a dated '
            '"nightly-2026-09-01") and build that revision.'
        ),
        context={"channel": toolchain.channel},
    )


def _not_a_toolchain(file: str, why: str) -> SourceError:
    return SourceError(
        f"{file} is not a Rust toolchain file tundravm can install: {why}.",
        source=file,
        reason=why,
        hint='Pin a release in it: [toolchain] channel = "1.91.0", then build that revision.',
    )


def host_rustup_version() -> str | None:
    """The version of the host's ``rustup`` (``1.28.2``), or ``None`` without one."""
    path = shutil.which("rustup")
    if path is None:
        return None
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=_NETWORK_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    words = completed.stdout.split()
    return words[1] if completed.returncode == 0 and len(words) > 1 else None


def require_host_rustup(toolchains: Mapping[str, RustToolchain]) -> None:
    """Refuse the Cargo builds *toolchains* maps to the toolchain they pin without ``rustup``.

    Raised once the checkouts are on the host and before any toolchain runs, since the
    offline sandbox cannot build a Cargo source whose toolchain the host did not install.
    """
    if not toolchains or host_rustup_version() is not None:
        return
    names = ", ".join(repr(name) for name in toolchains)
    channels = ", ".join(sorted({t.channel for t in toolchains.values()}))
    one = len(toolchains) == 1
    noun = f"Cargo source build {names} needs" if one else f"Cargo source builds {names} need"
    pins = "the Rust toolchain its rust-toolchain.toml pins" if one else "the Rust toolchains"
    raise StateError(
        f"{noun} rustup on the host to install {pins} ({channels}): there is no rustup on PATH.",
        hint=(
            "Install rustup (https://rustup.rs, or the Debian package rustup), then check the "
            "host with `tundravm doctor --for build`."
        ),
        context={"sources": ", ".join(toolchains), "toolchains": channels},
    )


def _rust_toolchain_files(name: str) -> tuple[str, ...]:
    """What the content manifest of toolchain *name* covers, relative to ``RUSTUP_HOME``."""
    root = f"toolchains/{name}"
    return (
        f"{root}/bin/cargo",
        f"{root}/bin/rustc",
        f"{root}/lib/rustlib/multirust-channel-manifest.toml",
        f"{root}/lib/rustlib/multirust-config.toml",
    )


def _install_rust_toolchain(
    build: SourceBuild, toolchain: RustToolchain, deps: Path
) -> tuple[dict[str, object], dict[str, str]]:
    """Install *toolchain* into ``<deps>/rustup`` with the host ``rustup``.

    Returns ``(record, contents)``: :func:`toolchain_record`'s record, with what its
    ``rustc -vV`` says, and the content manifest of its files (:func:`_rust_toolchain_files`).
    A failure raises ``SourceError`` naming the channel and the fix.
    """
    home = (deps / RUSTUP_DIRNAME).resolve()
    env = {**os.environ, "RUSTUP_HOME": str(home)}
    rustc = home / "toolchains" / toolchain.name / "bin" / "rustc"
    files = _rust_toolchain_files(toolchain.name)
    try:
        completed = run_tool(toolchain.install(), cwd=deps, env=env)
        if completed.returncode == 0:
            completed = run_tool((str(rustc), "-vV"), cwd=deps, env=env)
    except OSError as exc:
        reason = f"cannot run {exc.filename or 'rustup'}: {exc.strerror or exc}"
    else:
        lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
        reason = lines[-1] if lines else f"exit status {completed.returncode}"
        contents = {rel: entry for rel in files if (entry := _cache_entry(home / rel))}
        version, *rest = completed.stdout.splitlines() or [""]
        if completed.returncode == 0 and version and len(contents) == len(files):
            facts = dict(line.split(": ", 1) for line in rest if ": " in line)
            record: dict[str, object] = {
                "name": toolchain.name,
                "channel": toolchain.channel,
                "host": facts.get("host", RUST_HOST),
                "rustc": version.strip(),
                "commit": facts.get("commit-hash", ""),
                "profile": toolchain.profile,
                "components": list(toolchain.components),
                "targets": list(toolchain.targets),
            }
            return record, contents
        if completed.returncode == 0:
            reason = f"{toolchain.name} is not in {RUSTUP_DIRNAME}/toolchains after the install"
    raise SourceError(
        f"Cannot install Rust {toolchain.channel}, the toolchain source build {build.key!r}'s "
        f"{toolchain.file} pins: {reason}.",
        source=build.source.describe(),
        reason=reason,
        hint=(
            f"tundravm installs it on the host with `rustup toolchain install {toolchain.name}` "
            "from the Rust distribution server (RUSTUP_DIST_SERVER, static.rust-lang.org by "
            "default), then builds offline with it. Give the host access to it, or build a "
            "revision whose rust-toolchain.toml pins a released channel."
        ),
        context={"toolchain": toolchain.name, "file": toolchain.file},
    )


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
) -> str | None:
    """Fill ``<root>/deps/<cache>`` with *build*'s dependencies; the outcome, for the notice.

    The host's toolchain (``go mod download``, ``cargo fetch --locked``, ``dotnet
    restore --packages``) runs as the invoking user in a scratch copy of
    *checkout* (subdirectory included), so the checkout stays clean, after the host's
    ``rustup`` installed the toolchain a Cargo source pins into ``<root>/deps/rustup``
    (:func:`rust_toolchain`; its own ``cargo`` fetches), and a marker
    (:meth:`SourceBuild.deps_marker`) records the success with a content manifest
    (:func:`deps_problem`). Returns ``None`` for a script build, ``"<cache>: kept"``
    when the marker and its manifest still match, ``"<cache>: prefetched"`` (with
    ``(was incomplete: ...)`` when it repaired a cache), ``"<cache>: incomplete
    (...)"`` when an incomplete cache cannot be prefetched again, ``"skipped (no
    host <tool>)"`` when the toolchain is not on ``PATH``, ``"skipped (offline)"``
    under an offline policy, or ``"failed (<reason>)"``; the build then fetches
    its dependencies online.
    """
    marker = build.deps_marker(pin)
    if marker is None or not isinstance(build.build, LanguageBuild):
        return None
    recipe = build.build
    deps = root / DEPS_DIRNAME
    problem = deps_problem(build, pin, root)
    if problem is None:
        return f"{recipe.deps}: kept"
    incomplete = problem not in (None, NOT_PREFETCHED)
    rust = None if offline else checkout_rust_toolchain(build, checkout)
    tool = recipe.tool if rust is None else "rustup"
    if offline or shutil.which(tool) is None:
        if incomplete:
            return f"{recipe.deps}: incomplete ({problem})"
        return "skipped (offline)" if offline else f"skipped (no host {tool})"
    cache = deps / recipe.deps
    cache.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{build.pin_dir(pin)}.", dir=deps))
    toolchain: dict[str, object] | None = None
    files: dict[str, str] = {}
    try:
        tree = scratch / "tree"
        shutil.copytree(
            checkout, tree, symlinks=True, ignore=shutil.ignore_patterns(".git", FETCH_MARKER)
        )
        workdir = tree
        if isinstance(build.source, GitSource) and build.source.subdir:
            workdir = tree / build.source.subdir.strip("/")
        fetched: dict[str, str] = {}
        if isinstance(recipe, GoBuild):
            found = _go_mod_file(workdir, tree)
            name = go_toolchain(found.read_text(encoding="utf-8") if found else "")
            argv, env = recipe.prefetch(str(cache.resolve()), name)
            if name == GO_TOOLCHAIN_LOCAL:
                toolchain, _ = _go_toolchain_entries(name, cache)
            else:
                toolchain, fetched = _fetch_go_toolchain(build, name, workdir, cache, env)
            (deps / str(build.toolchain_file(pin))).write_text(f"{name}\n", encoding="utf-8")
        elif isinstance(recipe, CargoBuild):
            if rust is None:
                name = RUST_TOOLCHAIN_DEBIAN
                toolchain = {"name": name, "reason": _RUST_DEBIAN_REASON}
            else:
                name = rust.name
                toolchain, files = _install_rust_toolchain(build, rust, deps)
            argv, env = recipe.prefetch(str(cache.resolve()), name)
            if rust is not None:
                home = (deps / RUSTUP_DIRNAME).resolve()
                bin_dir = home / "toolchains" / name / "bin"
                env["RUSTUP_HOME"] = str(home)
                env["PATH"] = os.pathsep.join((str(bin_dir), os.environ.get("PATH", "")))
            (deps / str(build.toolchain_file(pin))).write_text(f"{name}\n", encoding="utf-8")
        else:
            argv, env = recipe.prefetch(str(cache.resolve()))
        try:
            completed = run_tool(argv, cwd=workdir, env={**os.environ, **env})
        except OSError as exc:
            return f"failed (cannot run {recipe.tool}: {exc.strerror or exc})"
        contents = dict(sorted({**_deps_contents(recipe, workdir, tree, cache), **fetched}.items()))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if completed.returncode != 0:
        lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
        last = lines[-1] if lines else f"exit status {completed.returncode}"
        return f"failed ({' '.join(argv[:2])}: {last})"
    record: dict[str, object] = {
        "build": build.key,
        "checkout": build.pin_dir(pin),
        "spec": build.deps_spec(),
        "contents": contents,
    }
    if toolchain is not None:
        record["toolchain"] = toolchain
    if files:
        record["toolchain_contents"] = files
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
            hint="Install git on the host that runs `tundravm lock` and `tundravm build`.",
        ) from exc
    if completed.returncode != 0:
        reason = _git_error(completed)
        raise SourceError(
            f"Cannot fetch {described}: {reason}.",
            source=described,
            reason=reason,
            hint=(
                "Check the repository URL and that git on this host can read it "
                "(credentials, network), then run `tundravm build` again."
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
            hint="The file changed upstream: `tundravm lock IMAGE --update NAME` pins it again.",
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
    if isinstance(source, LocalSource):
        return source.digest()
    if isinstance(source, GitSource):
        return _ls_remote(source)
    return _download_digest(source)


def _unreachable_hint(url: str) -> str:
    """What to do when git cannot read *url*: tundra-tools, which every image builds, is
    private, so its usual cause is a missing read access."""
    if "github.com/Hyodar/tundra-tools" in url:
        return (
            "tundra-tools is private: give git read access to github.com/Hyodar/tundra-tools "
            "(a credential helper or an SSH insteadOf), or build from a mirror with "
            "Tdxs(source=Git(mirror, ref))."
        )
    return "Check the repository URL, its access rights and the network."


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
            hint=_unreachable_hint(source.url),
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
    entries are reused. A local source is never resolved: its pin is its digest
    (:meth:`LocalSource.digest`), online or not. Every source is attempted; when any fails, one
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
        if isinstance(build.source, LocalSource):
            try:
                pins.append(build.locked(build.source.digest()))
            except SourceError as exc:
                failures[name] = exc
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
            "Run `tundravm lock` where the network reaches them, or pin the source inline "
            "(Git(url, ref) with a 40-hex commit ref, or Http(url, sha256=...))."
        )
    else:
        head = f"Cannot lock: {count} {noun} could not be resolved:"
        hints = dict.fromkeys(error.hint for error in failures.values() if error.hint)
        hint = " ".join(
            (*hints, "A source left out of `tundravm lock --update NAME` keeps its pin.")
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
    A local source whose directory no longer has the locked digest is changed, detailed
    as ``local-changed: <path> changed since the lock (<old7> -> <new7>)``; its digest
    is computed on the host, so this needs no *resolver*. Without *resolver* nothing
    touches the network.
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
        if isinstance(build.source, LocalSource) and build.matches(locked):
            digest = build.source.digest()
            if digest != locked.digest:
                changed.append(section)
                details[section] = (
                    f"{LOCAL_CHANGED}: {build.source.path} changed since the lock "
                    f"({locked.digest[:7]} -> {digest[:7]})"
                )
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
    "KernelFile",
    "KernelSource",
    "LOCAL_CHANGED",
    "LanguageBuild",
    "LocalSource",
    "NOT_PREFETCHED",
    "NamedSource",
    "Resolver",
    "RustToolchain",
    "ScriptBuild",
    "Source",
    "SourceBuild",
    "checkout_identity",
    "checkout_rust_toolchain",
    "copy_local",
    "default_resolver",
    "deps_problem",
    "fetch_source",
    "fingerprint",
    "go_toolchain",
    "host_go_version",
    "host_rustup_version",
    "is_fetched",
    "local_digest",
    "local_files",
    "local_manifest",
    "manifest_digest",
    "prefetch_deps",
    "require_host_go",
    "require_host_rustup",
    "resolve_pins",
    "rust_toolchain",
    "source_drift",
    "source_section",
    "toolchain_record",
    "write_marker",
]
