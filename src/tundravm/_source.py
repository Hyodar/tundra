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

import base64
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
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ._secret_scan import redact, url_credential
from .build_cache import Build, Cache, CacheDecl, CacheDir, CacheFile
from .errors import LockfileError, SourceError, StateError, TdxError, ValidationError
from .lockfile.model import LockedFetch, LockedToolchain
from .project import lock_command

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
SHA512_PATTERN = re.compile(r"^[0-9a-f]{128}$")
DOTNET_RELEASES_HOST = "builds.dotnet.microsoft.com"
"""Where the .NET release manifests and the SDK tarballs are served from."""
DOTNET_RELEASES_INDEX = (
    f"https://{DOTNET_RELEASES_HOST}/dotnet/release-metadata/releases-index.json"
)
"""The .NET release index: one entry per release (``9.0``), naming its ``releases.json``."""
DOTNET_GLOBAL_JSON = "global.json"
"""The file a .NET source names its SDK in, read as ``dotnet`` reads it."""
DOTNET_GLOBAL_JSON_PIN = "<global.json>"
"""The SDK version a cache fingerprint records before a lock resolved it."""
DOTNET_SDK_KIND = "dotnet-sdk"
"""The ``kind`` of a lockfile ``toolchains`` entry pinning a .NET SDK."""
DOTNET_DIRNAME = "dotnet"
"""``<out>/.sources/deps/dotnet/<version>``: where ``tundravm build`` unpacks each SDK."""
DOTNET_RID = "linux-x64"
"""The runtime identifier of every SDK ``tundravm build`` fetches: the build sandbox's."""
DOTNET_ROLL_FORWARD_DEFAULT = "latestPatch"
"""The ``rollForward`` of a global.json that names none, as ``dotnet`` reads it."""
_DOTNET_ROLL_FORWARD = ("disable", "patch", "feature", "latestPatch", "latestFeature")
"""The policies that keep the SDK within the .NET release ``sdk.version`` names."""
_DOTNET_ROLL_FORWARD_REFUSED = ("minor", "major", "latestMinor", "latestMajor")
"""The policies that reach past it: the SDK would move with each .NET release."""
_DOTNET_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
DOTNET_ENV = {
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_NOLOGO": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "DOTNET_CLI_USE_MSBUILD_SERVER": "0",
    "MSBUILDDISABLENODEREUSE": "1",
}
"""The environment of every ``dotnet`` tundravm runs: no telemetry, banner or first-run
setup, and no build server or MSBuild node outliving the build (one would hold the build
sandbox open)."""
DOTNET_PROPERTIES = {
    "Deterministic": "true",
    "ContinuousIntegrationBuild": "true",
    "NuGetAudit": "false",
    "UseSharedCompilation": "false",
}
"""The MSBuild properties of every restore and publish: deterministic output, no package
audit (it reads a moving advisory database over the network, so a project that treats
warnings as errors would fail its pinned build the day an advisory lands) and no compiler
server outliving the build."""


def refuse_url_credential(url: str, spelling: str) -> None:
    """Refuse a credential in the source URL *url* of the public *spelling* (``Git``).

    The URL is what the lock, the build manifest and the SBOM record; the message shows
    it redacted, never the credential.
    """
    found = url_credential(url)
    if found is None:
        return
    raise ValidationError(
        f'{spelling}("{redact(url)}"): the url carries {found}, which would reach the lock, '
        "the build manifest and the SBOM.",
        hint=f'{spelling}("<url without the credential>", ..., auth="GITHUB_TOKEN"), '
        "exporting GITHUB_TOKEN on the host that locks and builds; an SSH url "
        "(git@host:org/repo.git) reads the SSH agent instead.",
    )


_AUTH_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_AUTH_SCHEME = re.compile(r"^(?:bearer|basic:[^:\s]+)$")
Auth = str | tuple[str, str]
"""A source's ``auth=``: the environment variable holding the token, alone or, for a
``Git``, with the header scheme it travels in (``("CI_JOB_TOKEN", "basic:gitlab-ci-token")``)."""


def auth_variable(auth: Auth) -> str:
    """The environment variable an ``auth=`` names."""
    return auth if isinstance(auth, str) else auth[0]


def refuse_auth(url: str, auth: Auth | None, spelling: str) -> None:
    """Refuse an ``auth=`` that is not an environment variable name (with a header scheme,
    ``bearer`` or ``basic:USER``, for a ``Git``), or on a url that is not https: a token
    never travels in clear text, and an SSH url reads the agent."""
    if auth is None:
        return
    name = auth if isinstance(auth, str) else ""
    if spelling != "Git" and isinstance(auth, tuple):
        raise ValidationError(
            f"{spelling}(..., auth=(...)): a download sends its token as a bearer header, "
            "with no other scheme.",
            hint=f'{spelling}(url, ..., auth="TOKEN"), the variable alone.',
        )
    if isinstance(auth, tuple):
        if len(auth) != 2 or not all(isinstance(part, str) for part in auth):
            raise ValidationError(
                "Git(..., auth=(...)): auth= is the variable and the header scheme, two strings.",
                hint='Git(url, ref, auth=("CI_JOB_TOKEN", "basic:gitlab-ci-token")), or '
                'auth=("TOKEN", "bearer").',
            )
        if not _AUTH_SCHEME.fullmatch(auth[1]):
            raise ValidationError(
                f'Git(..., auth=("{auth[0]}", "{auth[1]}")): the scheme is not "bearer" or '
                '"basic:USER".',
                hint=f'Git(url, ref, auth=("{auth[0]}", "basic:<the user the host expects>")), '
                f'or auth=("{auth[0]}", "bearer").',
            )
        name = auth[0]
    if not _AUTH_NAME.fullmatch(name):
        raise ValidationError(
            f"{spelling}(..., auth=...): auth= names the environment variable holding the "
            "token, never the token.",
            hint=f'{spelling}(url, ..., auth="GITHUB_TOKEN"), exporting GITHUB_TOKEN on the '
            "host that locks and builds.",
        )
    if urlsplit(url).scheme.lower() != "https":
        shown = f'"{auth}"' if isinstance(auth, str) else f'("{auth[0]}", "{auth[1]}")'
        raise ValidationError(
            f'{spelling}("{redact(url)}", ..., auth={shown}): auth= sends the token over '
            "https only.",
            hint=f'{spelling}("https://...", ..., auth={shown}), or drop auth= for an SSH '
            "url (git@host:org/repo.git), which reads the SSH agent.",
        )


@dataclass(frozen=True, slots=True)
class GitSource:
    """A git repository at *ref* (branch, tag, or full 40-hex commit sha).

    *auth* names the host environment variable whose token git sends the source's host,
    alone or with its header scheme (:func:`git_auth_env`); it is read at each lock, fetch
    and prefetch and recorded nowhere, so it is no part of the payload or the checkout
    identity.
    """

    url: str
    ref: str
    subdir: str | None = field(default=None, kw_only=True)
    submodules: bool = field(default=False, kw_only=True)
    auth: Auth | None = field(default=None, kw_only=True)

    kind: Literal["git"] = field(default="git", init=False, repr=False)

    def __post_init__(self) -> None:
        refuse_url_credential(self.url, "Git")
        refuse_auth(self.url, self.auth, "Git")

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
    """A file or source archive at *url*; ``sha256`` pins it inline.

    *auth* names the host environment variable whose token the download sends *url*'s
    host as a bearer header (never another host, never across a redirect).
    """

    url: str
    sha256: str | None = field(default=None, kw_only=True)
    mirrors: tuple[str, ...] = field(default=(), kw_only=True)
    """URLs of the same file, read in order when *url* answers 404 (the sha256 pins it)."""
    auth: str | None = field(default=None, kw_only=True)

    kind: Literal["http"] = field(default="http", init=False, repr=False)

    def __post_init__(self) -> None:
        for url in (self.url, *self.mirrors):
            refuse_url_credential(url, "Http")
        refuse_auth(self.url, self.auth, "Http")

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
            hint=f"Run `{lock_command()}` to record it, review and commit the lockfile, then "
            "build.",
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
    """``go build -trimpath`` of *package* (the module root, ``"."``, by default) into
    ``<output_dir>/<output>``.

    ``output_dir`` is relative to the source tree. ``mkdir`` creates it before the
    build; ``go build -o`` creates it too, so turning it off only drops the step.
    ``reproducible`` adds ``-buildvcs=false``, ``CGO_ENABLED=0`` (unless ``cgo``
    or ``env`` sets it) and ``SOURCE_DATE_EPOCH`` (the build's, else ``0``); ``None``
    (the default) is on. The build runs the Go release the source's go.mod names
    (:func:`go_toolchain`), which ``tundravm build`` fetches on the host with the modules;
    ``env`` cannot set ``GOTOOLCHAIN``.
    """

    output: str
    package: str = "."
    ldflags: str = "-s -w -buildid="
    tags: Sequence[str] = ()
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    packages: Sequence[str] = ("golang",)
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

    def _prefetch(
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
    def _deps_env(cache: str) -> str:
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
    def _toolchain_env(record: str) -> str:
        """The export selecting the toolchain the prefetch wrote to *record*.

        Without a record (no host ``go``), ``local``: Debian's ``go``, which refuses a
        go.mod newer than itself instead of downloading a toolchain.
        """
        return f'export GOTOOLCHAIN="$(cat {record} 2>/dev/null || echo {GO_TOOLCHAIN_LOCAL})"'

    @property
    def artifact(self) -> str:
        """``<output_dir>/<output>``: the built binary, relative to the source tree."""
        return posixpath.normpath(f"{self.output_dir}/{self.output}")

    def command(self, workdir: str) -> str:
        """The build's shell command in *workdir*, the checkout's copy inside the sandbox."""
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
    features: Sequence[str] = ()
    profile: str = "release"
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    packages: Sequence[str] = ("cargo",)
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

    def _prefetch(
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
    def _deps_env(cache: str) -> str:
        """The exports pointing the in-build ``cargo`` at the copied cargo home *cache*."""
        return (
            f"export CARGO_HOME={cache} && "
            '{ [ "$WITH_NETWORK" != 0 ] || export CARGO_NET_OFFLINE=true; }'
        )

    @staticmethod
    def _toolchain_env(record: str, rustup: str) -> str:
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
        """``target/<profile>/<bin>``: the built binary, relative to the source tree."""
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
        """The build's shell command in *workdir*, the checkout's copy inside the sandbox."""
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

    The build runs the .NET SDK the source's ``global.json`` names (:func:`dotnet_sdk`):
    ``tundravm lock`` resolves it under the file's ``rollForward`` policy against the .NET
    release manifest and ``tundravm build`` downloads it on the host with the NuGet
    packages, so the sandbox builds offline and the host needs no ``dotnet`` of its own.
    ``packages`` are the Debian packages the SDK runs on, trixie's names: the C++ runtime,
    OpenSSL for NuGet's and the compiler's hashes, ICU for the compiler's generators.
    ``restore_args`` are appended to ``dotnet restore``; ``properties`` become
    ``-p:<name>=<value>`` on the restore and the publish, after :data:`DOTNET_PROPERTIES`.
    """

    project: str
    output: str
    configuration: str = "Release"
    runtime: str = DOTNET_RID
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    packages: Sequence[str] = ("libicu76", "libssl3t64", "libstdc++6")
    restore_args: Sequence[str] = field(default=(), metadata=_since(()))
    properties: Mapping[str, str] = field(default_factory=FrozenMap, metadata=_since({}))

    kind: Literal["dotnet"] = field(default="dotnet", init=False, repr=False)
    tool: ClassVar[str] = "dotnet"
    """The executable the prefetch runs: the fetched SDK's own, never the host's."""
    deps: ClassVar[str] = "nuget"
    """The dependency cache, ``<out>/.sources/deps/nuget``: a NuGet global packages folder."""

    def __post_init__(self) -> None:
        _freeze_recipe(self, ("env", "properties"), ("packages", "restore_args"))

    def _properties(self, *first: tuple[str, str]) -> tuple[str, ...]:
        """``-p:<name>=<value>`` for *first*, :data:`DOTNET_PROPERTIES` and the recipe's."""
        pairs = (*first, *DOTNET_PROPERTIES.items(), *self.properties.items())
        return tuple(f"-p:{_assignment(key, value)}" for key, value in pairs)

    def _restore_arguments(self) -> tuple[str, ...]:
        """What both restores take after ``--runtime``: the publish's properties (a
        self-contained single-file publish restores more than a plain restore, the ILLink
        tasks package for one), no node reuse, then ``restore_args``."""
        properties = self._properties(
            ("SelfContained", "true"), ("Configuration", self.configuration)
        )
        return (*properties, "-nodeReuse:false", *self.restore_args)

    def _prefetch(self, cache: str) -> tuple[tuple[str, ...], dict[str, str]]:
        """``dotnet restore`` of *project* for *runtime* into the packages folder *cache*."""
        argv = (
            "dotnet",
            "restore",
            self.project,
            "--runtime",
            self.runtime,
            "--packages",
            cache,
            *self._restore_arguments(),
        )
        return argv, {**DOTNET_ENV, **self.env}

    @staticmethod
    def _host_env(root: Path, scratch: Path) -> dict[str, str]:
        """The exports running the SDK unpacked at *root* on the host, its state under *scratch*
        (the first-run sentinel, NuGet's http and plugin caches); the packages folder and the
        user's NuGet configuration stay where they are."""
        return {
            "DOTNET_ROOT": str(root),
            "PATH": os.pathsep.join((str(root), os.environ.get("PATH", ""))),
            "DOTNET_CLI_HOME": str(scratch),
            "NUGET_HTTP_CACHE_PATH": str(scratch / "http-cache"),
            "NUGET_PLUGINS_CACHE_PATH": str(scratch / "plugins-cache"),
        }

    @staticmethod
    def _deps_env(cache: str) -> str:
        """The exports pointing the in-build ``dotnet`` at the copied packages *cache*.

        Without network a ``nuget.config`` naming the cache as the only package source
        replaces the project's (``RestoreConfigFile``, what ``--configfile`` sets): a
        project that maps packages to its sources would otherwise keep NuGet from
        considering the cache at all.
        """
        config = f"{cache}.config"
        xml = (
            '<configuration><packageSources><clear/><add key="tundravm" '
            f'value="{cache}"/></packageSources></configuration>'
        )
        return (
            f"export NUGET_PACKAGES={cache} && "
            f"{{ [ \"$WITH_NETWORK\" != 0 ] || {{ echo '{xml}' > {config} && "
            f"export RestoreConfigFile={config}; }}; }}"
        )

    @staticmethod
    def _toolchain_env(record: str, root: str) -> str:
        """The exports selecting the SDK the prefetch wrote to *record*, unpacked under *root*.

        Its ``dotnet`` comes first on ``PATH``, ``DOTNET_ROOT`` names it and its own
        directory holds the CLI's state. Without a record there is no ``dotnet`` at all
        (Debian ships none), and the build fails at its first ``dotnet``.
        """
        return (
            f'if [ -f {record} ]; then export DOTNET_ROOT={root}/"$(cat {record})" && '
            'export PATH="$DOTNET_ROOT:$PATH" DOTNET_CLI_HOME="$DOTNET_ROOT" '
            'NUGET_HTTP_CACHE_PATH="$DOTNET_ROOT/.nuget/http-cache" '
            'NUGET_PLUGINS_CACHE_PATH="$DOTNET_ROOT/.nuget/plugins-cache"; fi'
        )

    @property
    def artifact(self) -> str:
        """``publish/<output>``: the published program, relative to the source tree."""
        return f"publish/{self.output}"

    def command(self, workdir: str) -> str:
        """The build's shell command in *workdir*, the checkout's copy inside the sandbox."""
        env = _env_assignments({**DOTNET_ENV, **self.env})
        restore = " ".join(self._restore_arguments())
        publish = " ".join((*self._properties(), "-nodeReuse:false"))
        return (
            f"export {env} && cd {workdir} && "
            f"dotnet restore {self.project} --runtime {self.runtime} {restore} && "
            f"dotnet publish {self.project} --configuration {self.configuration} "
            f"--runtime {self.runtime} --self-contained true --no-restore "
            f"--output {workdir}/publish {publish}"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ScriptBuild:
    """Run *script* in the source tree; *output* is the produced file, relative to it.

    *env* is exported before the script runs.
    """

    script: str
    output: str
    packages: Sequence[str] = ()
    env: Mapping[str, str] = field(default_factory=FrozenMap, metadata=_since({}))

    kind: Literal["script"] = field(default="script", init=False, repr=False)

    def __post_init__(self) -> None:
        _freeze_recipe(self, ("env",), ("packages",))

    @property
    def artifact(self) -> str:
        return self.output

    def command(self, workdir: str) -> str:
        """The build's shell command in *workdir*, the checkout's copy inside the sandbox."""
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
        sdk: str | None = None,
    ) -> str:
        """The build-phase hook: copy the checkout, build, cache, install.

        *pin* is a lockfile pin; an inline pin (commit ref, ``sha256=``) wins. The hook
        copies the host-fetched checkout ``$SRCDIR/tundravm-sources/<name>-<pin[:12]>``;
        unpinned, it only fails, naming ``tundravm lock`` and ``tundravm build``.
        *distribution* (the variant's ``Lowered.build_distribution``) and *sdk* (the .NET
        SDK version the lock pins for a Dotnet build) enter the cache fingerprint.
        """
        return f"{self.marker()}\n{self._render(pin, distribution, sdk)}"

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

    def _render(
        self, pin: str | None, distribution: Mapping[str, object] | None, sdk: str | None
    ) -> str:
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
        fingerprint = self.cache_fingerprint(effective, distribution=distribution or {}, sdk=sdk)
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
        once, when ``tundravm build`` filled it, and the build's toolchain record
        (:meth:`toolchain_file`) beside it, with the Rust toolchain it names from
        ``deps/rustup`` or the .NET SDK it names from ``deps/dotnet``; *exports* (inside
        mkosi-chroot) point the toolchain at that copy, offline when mkosi runs the build
        without network, and select the build's toolchain. Both are empty for a script
        build.
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
        exports = f"if [ -d {chroot} ]; then {self.build._deps_env(str(chroot))}; fi && "
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
            return copy, exports + f"{GoBuild._toolchain_env(str(in_chroot))} && "
        name = f'"$(cat {mounted_record})"'
        if isinstance(self.build, DotnetBuild):
            copied = f'"{Build.build_path(f"{DEPS_BUILD_DIR}/{DOTNET_DIRNAME}")}"'
            copy += (
                f" && {{ [ ! -f {mounted_record} ] || [ -d {copied}/{name} ] || "
                f"{{ mkdir -p {copied} && cp -a --no-preserve=ownership "
                f'"{mounted_deps}/{DOTNET_DIRNAME}"/{name} {copied}/; }}; }}'
            )
            root = Build.chroot_path(f"{DEPS_BUILD_DIR}/{DOTNET_DIRNAME}")
            return copy, exports + f"{DotnetBuild._toolchain_env(str(in_chroot), str(root))} && "
        toolchains = f"{RUSTUP_DIRNAME}/toolchains"
        copied = f'"{Build.build_path(f"{DEPS_BUILD_DIR}/{toolchains}")}"'
        copy += (
            f" && {{ [ ! -f {mounted_record} ] || [ {name} = {RUST_TOOLCHAIN_DEBIAN} ] || "
            f"[ -d {copied}/{name} ] || {{ mkdir -p {copied} && "
            f'cp -a --no-preserve=ownership "{mounted_deps}/{toolchains}"/{name} {copied}/; }}; }}'
        )
        rustup = Build.chroot_path(f"{DEPS_BUILD_DIR}/{RUSTUP_DIRNAME}")
        return copy, exports + f"{CargoBuild._toolchain_env(str(in_chroot), str(rustup))} && "

    def toolchain_file(self, pin: str) -> str | None:
        """``<pin_dir>.<cache>.toolchain``: the toolchain a language build's prefetch selected.

        ``tundravm build`` writes it in ``.sources/deps`` once the host fetched that
        toolchain: a Go build's ``GOTOOLCHAIN`` (:func:`go_toolchain`), a Cargo build's
        installed Rust toolchain or ``debian`` (:func:`rust_toolchain`), a Dotnet build's
        SDK version (:func:`dotnet_sdk`). ``None`` for a script build.
        """
        if not isinstance(self.build, LanguageBuild):
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
        argv, env = self.build._prefetch(f"<{DEPS_DIRNAME}/{self.build.deps}>")
        return {
            "toolchain": self.build.kind,
            "command": list(argv),
            "env": dict(sorted(env.items())),
        }

    def cache_fingerprint(
        self, pin: str, *, distribution: Mapping[str, object], sdk: str | None = None
    ) -> str:
        """The first 16 hex of the sha256 over what a mounted build produces.

        Canonical JSON of the source pin (with git's ``subdir`` and
        ``submodules``), the build recipe (defaults decided, see
        :func:`resolve_recipe`) and install steps, the toolchain (recipe kind and packages, and
        that Go's go.mod or Rust's rust-toolchain.toml picks the release, or the .NET SDK
        version *sdk* the lock pins) and the *distribution* it comes from: base,
        architecture, mirror, snapshot, repositories, build packages and build settings
        (``Lowered.build_distribution``).
        """
        source: dict[str, object] = {"kind": self.source.kind, "pin": pin}
        if isinstance(self.source, GitSource):
            source.update(subdir=self.source.subdir, submodules=self.source.submodules)
        toolchain = {
            "kind": self.build.kind,
            "packages": list(self.build_packages()),
            **_TOOLCHAIN_VERSIONS.get(self.build.kind, {}),
        }
        if sdk is not None and isinstance(self.build, DotnetBuild):
            toolchain["version"] = sdk
        spec = {
            "source": source,
            "build": _recipe_payload(resolve_recipe(self.build)),
            "install": [
                {"kind": step.kind, "path": step.path, "dest": step.dest, "mode": step.mode}
                for step in self.install
            ],
            "distribution": dict(distribution),
            "toolchain": toolchain,
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
    "dotnet": {"version": DOTNET_GLOBAL_JSON_PIN},
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
    if source.subdir:
        _require_subdir(source, pin, dest)


def _require_subdir(source: GitSource, pin: str, checkout: Path) -> None:
    """Refuse a checkout of *pin* without *source*'s ``subdir`` as a directory inside it."""
    subdir = source.subdir or ""
    workdir = (checkout / subdir).resolve()
    if workdir.is_dir() and workdir.is_relative_to(checkout.resolve()):
        return
    reason = f"{pin[:12]} has no directory {subdir} inside the checkout"
    raise SourceError(
        f"Cannot build {source.describe()}: {reason}.",
        source=source.describe(),
        reason=reason,
        hint=f"Pass subdir= a directory of the repository at that commit, not {subdir!r}.",
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


_LANGUAGES = {"go": "Go", "cargo": "Rust", "dotnet": ".NET"}
"""The language of each recipe kind's toolchain, as the deps problems name it."""


def deps_problem(
    build: SourceBuild, pin: str, root: Path, *, sdk: LockedToolchain | None = None
) -> str | None:
    """Why ``<root>/deps`` lacks *build*'s dependencies for *pin*, or ``None`` when complete.

    *root* is ``<out>/.sources``. Complete means the marker records this
    prefetch command and every entry of its content manifest is still there:
    each Go module directory and ``.mod`` file ``go.sum`` names, each ``.crate``
    of ``Cargo.lock`` (size and sha256), each NuGet package directory of the
    restore's ``project.assets.json``, and the toolchain record naming the recorded
    toolchain, a Rust one with its files in ``deps/rustup``, a .NET SDK with its files in
    ``deps/dotnet`` and, with *sdk* (the lock's pin), the version and sha512 the lock
    pins. ``None`` for a script build, which has none; :data:`NOT_PREFETCHED` without a
    marker.
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
    language = _LANGUAGES[build.build.kind]
    if named is None or written != named or not isinstance(toolchain, dict):
        return f"{DEPS_DIRNAME}/{record} does not name the recorded {language} toolchain"
    files = recorded.get("toolchain_contents")
    if isinstance(build.build, CargoBuild) and named != RUST_TOOLCHAIN_DEBIAN:
        home, what = RUSTUP_DIRNAME, "Rust toolchain"
    elif isinstance(build.build, DotnetBuild):
        recorded_sdk = (toolchain.get("version"), toolchain.get("sha512"))
        if sdk is not None and recorded_sdk != (sdk.version, sdk.sha512):
            return f"prefetched with .NET SDK {named}, not the {sdk.version} the lock pins"
        home, what = DOTNET_DIRNAME, ".NET SDK"
    else:
        return None
    for rel, expected in sorted(files.items() if isinstance(files, dict) else ()):
        if _cache_entry(root / DEPS_DIRNAME / home / rel) != expected:
            return f"{DEPS_DIRNAME}/{home}/{rel} is missing or changed"
    if not files:
        return f"the marker has no content manifest of the {what}"
    return None


def toolchain_record(build: SourceBuild, pin: str, root: Path) -> dict[str, object] | None:
    """The toolchain the prefetch of *build* at *pin* recorded in ``<root>/deps``.

    Go: ``{"name", "module", "sum"}``, the ``GOTOOLCHAIN`` release, its module and the
    ``h1:`` checksum the checksum database vouched for, or ``{"name": "local", "reason"}``
    for a go.mod older than toolchain modules. Rust: :meth:`RustToolchain.record` with
    what ``rustc -vV`` says, or ``{"name": "debian", "reason"}`` for a source that pins
    none. .NET: ``{"name", "version", "requested", "url", "sha512"}``, the SDK release the
    source's global.json resolved to and the tarball it came from. ``None`` for a script
    build or one that no prefetch recorded.
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
        f"{noun} Go 1.21 or newer on the host to fetch the toolchain and modules each "
        f"go.mod names: {have}.",
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


@dataclass(frozen=True, slots=True)
class DotnetSdk:
    """The .NET SDK a source's ``global.json`` asks for: *version* under its *roll_forward*.

    *file* is the ``global.json`` it comes from, relative to the source tree.
    *allow_prerelease* is the file's ``allowPrerelease``, ``False`` unless it says so: a
    pinned build takes no preview SDK it did not ask for.
    """

    version: str
    roll_forward: str = DOTNET_ROLL_FORWARD_DEFAULT
    allow_prerelease: bool = False
    file: str = DOTNET_GLOBAL_JSON

    @property
    def channel(self) -> str:
        """``<major>.<minor>``: the .NET release the SDK belongs to (``9.0``)."""
        major, minor, *_ = self.version.split(".")
        return f"{major}.{minor}"

    @property
    def requested(self) -> str:
        """``<version> <rollForward>``: what the lock records the pin was resolved from."""
        return f"{self.version} {self.roll_forward}"


@dataclass(frozen=True, slots=True)
class DotnetRelease:
    """An SDK of the .NET release manifest: its *version*, its ``linux-x64`` tarball's *url*
    and the tarball's *sha512*, as the manifest states them."""

    version: str
    url: str
    sha512: str


def dotnet_sdk(workdir: Path, tree: Path, described: str) -> DotnetSdk:
    """The SDK the source tree *tree* names for a build in *workdir*: its ``global.json``.

    The file is looked for from *workdir* up to *tree*, as ``dotnet`` looks for it. A
    source without one, or one naming no ``sdk.version`` or a ``rollForward`` that reaches
    past the .NET release the version names, raises ``SourceError`` saying what to write;
    *described* names the source (``git <url> @ <ref>``).
    """
    for directory in _up_to(workdir, tree):
        path = directory / DOTNET_GLOBAL_JSON
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            return _dotnet_global_json(text, path.relative_to(tree).as_posix(), described)
    reason = f"no {DOTNET_GLOBAL_JSON}"
    raise SourceError(
        f"{described} has no {DOTNET_GLOBAL_JSON}, so no .NET SDK can be fetched for it "
        "(Debian ships none).",
        source=described,
        reason=reason,
        hint=(
            f"Add {DOTNET_GLOBAL_JSON} to the source "
            '({"sdk": {"version": "9.0.100", "rollForward": "latestPatch"}}) and build that '
            "revision."
        ),
    )


def checkout_dotnet_sdk(build: SourceBuild, checkout: Path) -> DotnetSdk:
    """:func:`dotnet_sdk` of a Dotnet *build*'s *checkout* (its subdirectory, for a git
    source that names one)."""
    workdir = checkout
    if isinstance(build.source, GitSource) and build.source.subdir:
        workdir = checkout / build.source.subdir.strip("/")
    return dotnet_sdk(workdir, checkout, build.source.describe())


def _dotnet_global_json(text: str, file: str, described: str) -> DotnetSdk:
    try:
        table: object = json.loads(text)
    except ValueError as exc:
        raise _not_a_global_json(file, described, f"it is not JSON ({exc})") from None
    sdk = table.get("sdk") if isinstance(table, dict) else None
    if not isinstance(sdk, dict):
        raise _not_a_global_json(file, described, "it has no sdk object")
    version = sdk.get("version")
    if not isinstance(version, str) or not version:
        raise _not_a_global_json(file, described, "its sdk names no version")
    if _dotnet_version_key(version) is None:
        raise _not_a_global_json(
            file, described, f"sdk.version {version!r} is not a full SDK version (x.y.znn)"
        )
    policy = sdk.get("rollForward", DOTNET_ROLL_FORWARD_DEFAULT)
    channel = DotnetSdk(version).channel
    if policy in _DOTNET_ROLL_FORWARD_REFUSED:
        reason = f"rollForward {policy} reaches past .NET {channel}"
        raise SourceError(
            f"{file} of {described} asks for rollForward {policy}, which reaches past .NET "
            f"{channel}: the SDK would move with each .NET release.",
            source=described,
            reason=reason,
            hint=(
                f'Keep the SDK within .NET {channel} in {file} ("rollForward": '
                '"latestFeature") and build that revision.'
            ),
        )
    if policy not in _DOTNET_ROLL_FORWARD:
        raise _not_a_global_json(file, described, f"rollForward {policy!r} is not a policy")
    prerelease = sdk.get("allowPrerelease", False)
    if not isinstance(prerelease, bool):
        raise _not_a_global_json(file, described, "its allowPrerelease is not true or false")
    return DotnetSdk(version, roll_forward=str(policy), allow_prerelease=prerelease, file=file)


def _not_a_global_json(file: str, described: str, why: str) -> SourceError:
    return SourceError(
        f"{file} of {described} is not a global.json tundravm can read: {why}.",
        source=described,
        reason=why,
        hint=(
            f'Name a released SDK in {file} ({{"sdk": {{"version": "9.0.100", "rollForward": '
            '"latestPatch"}}) and build that revision.'
        ),
    )


type _DotnetKey = tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]


def _dotnet_version_key(version: str) -> _DotnetKey | None:
    """An SDK version's sort key: ``9.0.100-rc.2.24474.11`` < ``9.0.100`` < ``9.0.200``."""
    match = _DOTNET_VERSION.fullmatch(version)
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    tail: tuple[tuple[int, int | str], ...] = ()
    if pre is not None:
        tail = tuple((0, int(part)) if part.isdigit() else (1, part) for part in pre.split("."))
    return int(major), int(minor), int(patch), 0 if pre else 1, tail


def _dotnet_sdks(releases: Mapping[str, object]) -> dict[str, DotnetRelease]:
    """Every SDK of a release's ``releases.json`` with a ``linux-x64`` tarball, by version."""
    found: dict[str, DotnetRelease] = {}
    entries = releases.get("releases")
    for release in entries if isinstance(entries, list) else []:
        if not isinstance(release, dict):
            continue
        sdks = release.get("sdks")
        for sdk in sdks if isinstance(sdks, list) else [release.get("sdk")]:
            if not isinstance(sdk, dict):
                continue
            version, files = sdk.get("version"), sdk.get("files")
            if not isinstance(version, str) or not isinstance(files, list):
                continue
            for file in files:
                if not isinstance(file, dict) or file.get("rid") != DOTNET_RID:
                    continue
                url, digest = file.get("url"), file.get("hash")
                if not str(file.get("name", "")).endswith(".tar.gz"):
                    continue
                if isinstance(url, str) and isinstance(digest, str):
                    if SHA512_PATTERN.fullmatch(digest):
                        found.setdefault(version, DotnetRelease(version, url, digest))
    return found


def select_dotnet_sdk(
    request: DotnetSdk, releases: Mapping[str, object], described: str
) -> DotnetRelease:
    """The SDK of the release manifest *releases* (``releases.json``) satisfying *request*.

    As ``dotnet`` selects among installed SDKs, over every SDK the release published:
    ``disable`` the version itself; ``patch`` it, else the latest patch of its feature
    band; ``latestPatch`` the latest patch of its band; ``feature`` the latest patch of its
    band, else of the next band; ``latestFeature`` the latest SDK of the release. A
    prerelease SDK counts only when the file allows prereleases or asks for one. Raises
    ``SourceError`` naming the release's latest SDK when nothing satisfies the request.
    """
    available = _dotnet_sdks(releases)
    parsed = _dotnet_version_key(request.version)
    assert parsed is not None  # DotnetSdk holds a parsed version
    wanted: _DotnetKey = parsed
    prerelease = request.allow_prerelease or not wanted[3]
    keys = {v: k for v in available if (k := _dotnet_version_key(v)) and (prerelease or k[3])}
    major, minor, patch = wanted[:3]
    band = patch // 100
    in_release = {v: k for v, k in keys.items() if k[:2] == (major, minor)}
    in_band = {v: k for v, k in in_release.items() if k[2] // 100 == band}

    def latest(candidates: Mapping[str, _DotnetKey], floor: _DotnetKey = wanted) -> str | None:
        eligible = {v: k for v, k in candidates.items() if k >= floor}
        return max(eligible, key=lambda v: eligible[v]) if eligible else None

    policy = request.roll_forward
    exact = request.version if request.version in keys else None
    if policy == "disable":
        chosen = exact
    elif policy == "patch":
        chosen = exact or latest(in_band)
    elif policy == "latestPatch":
        chosen = latest(in_band)
    elif policy == "feature":
        chosen = latest(in_band)
        bands = sorted({k[2] // 100 for k in in_release.values() if k[2] // 100 > band})
        if chosen is None and bands:
            next_band = {v: k for v, k in in_release.items() if k[2] // 100 == bands[0]}
            chosen = latest(next_band, (major, minor, bands[0] * 100, 0, ()))
    else:
        chosen = latest(in_release)
    if chosen is None:
        reason = f"no .NET {request.channel} SDK satisfies {request.requested}"
        raise SourceError(
            f"{request.file} of {described} asks for .NET SDK {request.version} (rollForward "
            f"{policy}), which no SDK of .NET {request.channel} satisfies.",
            source=described,
            reason=reason,
            hint=(
                f"Name an SDK .NET {request.channel} released in {request.file} (its latest "
                f"is {releases.get('latest-sdk')}), or a policy that reaches one "
                '("rollForward": "latestFeature"), and build that revision.'
            ),
        )
    return available[chosen]


def read_dotnet_manifest(url: str) -> object:
    """The JSON at *url*, a .NET release manifest (tests replace it)."""
    with urlopen(url, timeout=_NETWORK_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def resolve_dotnet_sdk(request: DotnetSdk, described: str) -> DotnetRelease:
    """*request*'s SDK release: the .NET release index names the release's ``releases.json``,
    which :func:`select_dotnet_sdk` resolves it against. A manifest that cannot be read
    raises ``SourceError`` naming the host to reach."""
    try:
        index = read_dotnet_manifest(DOTNET_RELEASES_INDEX)
        releases = read_dotnet_manifest(_dotnet_releases_url(index, request, described))
    except HTTPError as exc:
        exc.close()
        raise _manifest_unreadable(request, described, f"HTTP {exc.code}") from exc
    except (OSError, ValueError) as exc:
        reason = _network_reason(exc.reason if isinstance(exc, URLError) else exc)
        raise _manifest_unreadable(request, described, reason) from exc
    if not isinstance(releases, dict):
        raise _manifest_unreadable(request, described, "releases.json is not a JSON object")
    return select_dotnet_sdk(request, releases, described)


def _dotnet_releases_url(index: object, request: DotnetSdk, described: str) -> str:
    """The ``releases.json`` of *request*'s release, as the release *index* names it."""
    entries = index.get("releases-index") if isinstance(index, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("channel-version") != request.channel:
            continue
        url = entry.get("releases.json")
        if isinstance(url, str):
            return url
    reason = f"no .NET {request.channel} in the release index"
    raise SourceError(
        f"{request.file} of {described} asks for .NET SDK {request.version}, and the .NET "
        f"release index lists no .NET {request.channel}.",
        source=described,
        reason=reason,
        hint=f"Name an SDK of a published .NET release in {request.file} and build that revision.",
    )


def _manifest_unreadable(request: DotnetSdk, described: str, reason: str) -> SourceError:
    return SourceError(
        f"Cannot resolve the .NET SDK {described}'s {request.file} asks for: {reason}.",
        source=described,
        reason=reason,
        hint=f"Check the network path to {DOTNET_RELEASES_HOST}, then lock again.",
    )


def locked_dotnet_sdk(
    name: str, pin: str, request: DotnetSdk, release: DotnetRelease
) -> LockedToolchain:
    """The lockfile ``toolchains`` entry pinning *release* for source build *name* at *pin*."""
    return LockedToolchain(
        name=name,
        kind=DOTNET_SDK_KIND,
        source=pin,
        requested=request.requested,
        version=release.version,
        url=release.url,
        sha512=release.sha512,
    )


def lock_dotnet_sdk(build: SourceBuild, pin: str, checkout: Path) -> LockedToolchain:
    """The SDK pin of a Dotnet *build* at *pin*: its *checkout*'s ``global.json``, resolved
    against the release manifest over the network (tests replace it)."""
    request = checkout_dotnet_sdk(build, checkout)
    release = resolve_dotnet_sdk(request, build.source.describe())
    return locked_dotnet_sdk(build.key, pin, request, release)


DOTNET_SDK_MARKER = ".tundravm-sha512"
"""The file an unpacked SDK holds naming the sha512 of the tarball it came from."""


def _dotnet_sdk_files(version: str) -> tuple[str, ...]:
    """What the content manifest of SDK *version* covers, relative to ``deps/dotnet``."""
    return (
        f"{version}/dotnet",
        f"{version}/sdk/{version}/dotnet.dll",
        f"{version}/sdk/{version}/.version",
        f"{version}/{DOTNET_SDK_MARKER}",
    )


def _dotnet_record(sdk: LockedToolchain) -> dict[str, object]:
    return {
        "name": sdk.version,
        "version": sdk.version,
        "requested": sdk.requested,
        "url": sdk.url,
        "sha512": sdk.sha512,
    }


def _fetch_dotnet_sdk(
    build: SourceBuild, sdk: LockedToolchain, deps: Path
) -> tuple[dict[str, object], dict[str, str]]:
    """Download *sdk*'s tarball and unpack it into ``<deps>/dotnet/<version>``, once.

    The download is checked against the sha512 the lock recorded from the release
    manifest before anything is unpacked. Returns ``(record, contents)``:
    :func:`toolchain_record`'s record and the content manifest of the SDK's files
    (:func:`_dotnet_sdk_files`). A failure raises ``SourceError`` naming the release and
    the fix.
    """
    root = deps / DOTNET_DIRNAME
    target = root / sdk.version
    files = _dotnet_sdk_files(sdk.version)
    contents = {rel: entry for rel in files if (entry := _cache_entry(root / rel))}
    try:
        unpacked_from = (target / DOTNET_SDK_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        unpacked_from = None
    if len(contents) == len(files) and unpacked_from == sdk.sha512:
        return _dotnet_record(sdk), contents
    root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{sdk.version}.", dir=root))
    try:
        reason = _download_dotnet_sdk(sdk, scratch)
        if reason is None:
            if target.exists():
                shutil.rmtree(target)
            (scratch / "root").rename(target)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if reason is None:
        contents = {rel: entry for rel in files if (entry := _cache_entry(root / rel))}
        if len(contents) == len(files):
            return _dotnet_record(sdk), contents
        missing = next(rel for rel in files if rel not in contents).split("/", 1)[1]
        reason = f"the tarball holds no {missing}, so it is not SDK {sdk.version}"
    raise SourceError(
        f"Cannot fetch .NET SDK {sdk.version}, the one source build {build.key!r}'s global.json "
        f"resolves to: {reason}.",
        source=build.source.describe(),
        reason=reason,
        hint=(
            f"tundravm downloads it on the host from {sdk.url}, checks its sha512 against the "
            f"lock and builds offline with it. Give the host access to {DOTNET_RELEASES_HOST}, "
            "or lock again if the release was republished."
        ),
        context={"toolchain": sdk.version, "url": sdk.url},
    )


def _download_dotnet_sdk(sdk: LockedToolchain, scratch: Path) -> str | None:
    """Download and unpack *sdk* under *scratch* (``root/``); the reason when that fails."""
    archive = scratch / "sdk.tar.gz"
    digest = hashlib.sha512()
    try:
        with urlopen(sdk.url, timeout=_NETWORK_TIMEOUT) as response, archive.open("wb") as out:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
    except HTTPError as exc:
        exc.close()
        return f"HTTP {exc.code}"
    except OSError as exc:
        return _network_reason(exc.reason if isinstance(exc, URLError) else exc)
    if digest.hexdigest() != sdk.sha512:
        return f"sha512 {digest.hexdigest()[:12]} does not match the lock's {sdk.sha512[:12]}"
    unpacked = scratch / "root"
    unpacked.mkdir()
    unpack = ["tar", "-xzf", str(archive), "-C", str(unpacked)]
    completed = subprocess.run(unpack, check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        lines = completed.stderr.strip().splitlines()
        return f"cannot unpack the tarball: {lines[-1] if lines else 'tar failed'}"
    archive.unlink()
    (unpacked / DOTNET_SDK_MARKER).write_text(f"{sdk.sha512}\n", encoding="utf-8")
    return None


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
    sdk: LockedToolchain | None = None,
) -> str | None:
    """Fill ``<root>/deps/<cache>`` with *build*'s dependencies; the outcome, for the notice.

    The host's toolchain (``go mod download``, ``cargo fetch --locked``, ``dotnet
    restore --packages``) runs as the invoking user in a scratch copy of
    *checkout* (subdirectory included), so the checkout stays clean, after the host's
    ``rustup`` installed the toolchain a Cargo source pins into ``<root>/deps/rustup``
    (:func:`rust_toolchain`; its own ``cargo`` fetches) or the .NET SDK the lock pins as
    *sdk* (else the one the checkout's ``global.json`` resolves to now,
    :func:`lock_dotnet_sdk`) was unpacked into ``<root>/deps/dotnet`` (its own
    ``dotnet`` restores), and a marker (:meth:`SourceBuild.deps_marker`) records the
    success with a content manifest (:func:`deps_problem`). Returns ``None`` for a script
    build, ``"<cache>: kept"`` when the marker and its manifest still match,
    ``"<cache>: prefetched"`` (with ``(was incomplete: ...)`` when it repaired a cache),
    ``"<cache>: incomplete (...)"`` when an incomplete cache cannot be prefetched again,
    ``"skipped (no host <tool>)"`` when the toolchain is not on ``PATH``, ``"skipped
    (offline)"`` under an offline policy, or ``"failed (<reason>)"``; the build then
    fetches its dependencies online.
    """
    marker = build.deps_marker(pin)
    if marker is None or not isinstance(build.build, LanguageBuild):
        return None
    recipe = build.build
    deps = root / DEPS_DIRNAME
    problem = deps_problem(build, pin, root, sdk=sdk)
    if problem is None:
        return f"{recipe.deps}: kept"
    incomplete = problem not in (None, NOT_PREFETCHED)
    rust = None if offline else checkout_rust_toolchain(build, checkout)
    tool = recipe.tool if rust is None else "rustup"
    fetched_tool = isinstance(recipe, DotnetBuild)
    if offline or (not fetched_tool and shutil.which(tool) is None):
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
            argv, env = recipe._prefetch(str(cache.resolve()), name)
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
            argv, env = recipe._prefetch(str(cache.resolve()), name)
            if rust is not None:
                home = (deps / RUSTUP_DIRNAME).resolve()
                bin_dir = home / "toolchains" / name / "bin"
                env["RUSTUP_HOME"] = str(home)
                env["PATH"] = os.pathsep.join((str(bin_dir), os.environ.get("PATH", "")))
            (deps / str(build.toolchain_file(pin))).write_text(f"{name}\n", encoding="utf-8")
        else:
            if sdk is None:
                sdk = lock_dotnet_sdk(build, pin, checkout)
            toolchain, files = _fetch_dotnet_sdk(build, sdk, deps)
            argv, env = recipe._prefetch(str(cache.resolve()))
            sdk_root = (deps / DOTNET_DIRNAME / sdk.version).resolve()
            env.update(DotnetBuild._host_env(sdk_root, scratch / "home"))
            name = sdk.version
            (deps / str(build.toolchain_file(pin))).write_text(f"{name}\n", encoding="utf-8")
        if isinstance(build.source, GitSource):
            try:
                env.update(_git_deps_auth_env(recipe, build.source))
            except SourceError as exc:
                return f"failed ({exc.reason})"
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


_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})


def _git_deps_auth_env(recipe: LanguageBuild, source: GitSource) -> dict[str, str]:
    """The prefetch environment giving *source*'s ``auth`` token to the git dependencies
    on its host: :func:`git_auth_env`, which ``go mod download`` and ``dotnet restore``
    (an MSBuild task that runs git) pass to the git they run.

    Cargo fetches git dependencies with its built-in libgit2, which reads a credential
    helper and the SSH agent but neither ``GIT_CONFIG_*`` nor ``http.extraHeader``, so
    ``CARGO_NET_GIT_FETCH_WITH_CLI`` has it run git instead.
    """
    env = git_auth_env(source)
    if env and isinstance(recipe, CargoBuild):
        env["CARGO_NET_GIT_FETCH_WITH_CLI"] = "true"
    return env


def _auth_token(source: GitSource | HttpSource) -> str | None:
    """The value of *source*'s ``auth`` variable on this host, read now; ``None`` without one.

    Raises :class:`~tundravm.errors.SourceError` naming the variable when it is unset or
    empty.
    """
    if source.auth is None:
        return None
    variable = auth_variable(source.auth)
    token = os.environ.get(variable, "")
    if token:
        return token
    described = source.describe()
    reason = f"the environment variable {variable} (its auth=) is not set"
    raise SourceError(
        f"Cannot fetch {described}: {reason}.",
        source=described,
        reason=reason,
        hint=f"export {variable}=<a token that reads the source> on this host, then run "
        "`tundravm lock` or `tundravm build` again.",
    )


def _git_authorization(host: str, token: str, scheme: str | None) -> str:
    """The ``Authorization`` value git sends a *host* that reads *token*.

    *scheme* is the one ``auth=`` states: ``bearer``, or ``basic:USER`` with *token* as
    the password. Without one, github.com and a ``gitlab.*`` host take the token as a
    basic-auth password (``x-access-token`` and ``oauth2`` as the user, as their CI
    checkouts send it), and any other host a bearer token.
    """
    if scheme is None:
        if host in _GITHUB_HOSTS:
            scheme = "basic:x-access-token"
        elif host == "gitlab.com" or host.startswith("gitlab."):
            scheme = "basic:oauth2"
        else:
            scheme = "bearer"
    if scheme == "bearer":
        return f"Bearer {token}"
    user = scheme.removeprefix("basic:")
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode("ascii")


def git_auth_env(source: GitSource) -> dict[str, str]:
    """The environment that has git send *source*'s ``auth`` token to the source's host.

    One ``http.<scheme>://<host>/.extraHeader`` entry appended to the ``GIT_CONFIG_*``
    entries already in the environment, so the token is never in argv, a config file or a
    credential store, and reaches the source's submodules and the Go and Cargo git
    dependencies on the same host. Empty without ``auth``.
    """
    token = _auth_token(source)
    if token is None:
        return {}
    parts = urlsplit(source.url)
    host = (parts.hostname or "").lower()
    scheme = source.auth[1] if isinstance(source.auth, tuple) else None
    count = os.environ.get("GIT_CONFIG_COUNT", "0")
    index = int(count) if count.isdigit() else 0
    return {
        "GIT_CONFIG_COUNT": str(index + 1),
        f"GIT_CONFIG_KEY_{index}": f"http.{parts.scheme}://{parts.netloc}/.extraHeader",
        f"GIT_CONFIG_VALUE_{index}": f"Authorization: {_git_authorization(host, token, scheme)}",
    }


def _git(source: GitSource, *args: str) -> None:
    described = source.describe()
    command = ["git", *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **git_auth_env(source)}
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            env=env,
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


def _request(source: HttpSource, url: str) -> Request:
    """The request for *url*: with *source*'s ``auth`` token as a bearer header when *url*
    is on the source's own host, and never on a redirect (a release asset redirects to a
    storage host that must not see it)."""
    request = Request(url)
    token = _auth_token(source)
    if token is not None and urlsplit(url).netloc == urlsplit(source.url).netloc:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    return request


def _open(source: HttpSource) -> Any:
    """The response of *source*'s url, else of the first mirror that does not answer 404."""
    *first, last = (source.url, *source.mirrors)
    for url in first:
        try:
            return urlopen(_request(source, url), timeout=_NETWORK_TIMEOUT)
        except HTTPError as exc:
            exc.close()
            if exc.code != 404:
                raise
    return urlopen(_request(source, last), timeout=_NETWORK_TIMEOUT)


def _download(source: HttpSource, pin: str, dest: Path) -> None:
    described = source.describe()
    file = dest / source.filename
    digest = hashlib.sha256()
    try:
        with _open(source) as response, file.open("wb") as out:
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
            hint=f"The file changed upstream: `{lock_command('--update', 'NAME')}` pins it again.",
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


def _ls_remote(source: GitSource) -> str:
    """The commit *source*'s ref points at, without prompting for credentials."""
    command = ["git", "ls-remote", source.url, source.ref]
    described = source.describe()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **git_auth_env(source)}
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=_NETWORK_TIMEOUT,
            env=env,
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
        with _open(source) as response:
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
    "DotnetRelease",
    "DotnetSdk",
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
    "checkout_dotnet_sdk",
    "checkout_identity",
    "checkout_rust_toolchain",
    "copy_local",
    "default_resolver",
    "deps_problem",
    "dotnet_sdk",
    "fetch_source",
    "fingerprint",
    "go_toolchain",
    "host_go_version",
    "host_rustup_version",
    "is_fetched",
    "local_digest",
    "local_files",
    "local_manifest",
    "lock_dotnet_sdk",
    "locked_dotnet_sdk",
    "manifest_digest",
    "prefetch_deps",
    "read_dotnet_manifest",
    "require_host_go",
    "require_host_rustup",
    "resolve_dotnet_sdk",
    "resolve_pins",
    "rust_toolchain",
    "select_dotnet_sdk",
    "source_drift",
    "source_section",
    "toolchain_record",
    "write_marker",
]
