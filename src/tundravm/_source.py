"""Source builds: a fetched source plus a build recipe, pinned through the lockfile.

``declarative.lower`` turns each declarative ``Build`` into a :class:`SourceBuild`
and hands it to ``Image.build_from``; the internal modules declare theirs directly.

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
import os
import posixpath
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Literal
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
    env: Mapping[str, str] = field(default_factory=dict)
    packages: tuple[str, ...] = ("golang",)
    output_dir: str = field(default="./build", metadata=_since("./build"))
    mkdir: bool = field(default=True, metadata=_since(True))

    kind: Literal["go"] = field(default="go", init=False, repr=False)

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
    env: Mapping[str, str] = field(default_factory=dict)
    packages: tuple[str, ...] = ("cargo",)

    kind: Literal["cargo"] = field(default="cargo", init=False, repr=False)

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
    env: Mapping[str, str] = field(default_factory=dict)
    packages: tuple[str, ...] = ("dotnet-sdk-8.0",)
    restore_args: tuple[str, ...] = field(default=(), metadata=_since(()))
    properties: Mapping[str, str] = field(default_factory=dict, metadata=_since({}))

    kind: Literal["dotnet"] = field(default="dotnet", init=False, repr=False)

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
    env: Mapping[str, str] = field(default_factory=dict, metadata=_since({}))

    kind: Literal["script"] = field(default="script", init=False, repr=False)

    @property
    def artifact(self) -> str:
        return self.output

    def command(self, workdir: str) -> str:
        env = f"export {_env_assignments(self.env)} && " if self.env else ""
        return f"{env}cd {workdir} && {self.script}"


BuildRecipe = GoBuild | CargoBuild | DotnetBuild | ScriptBuild


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
    the lockfile entry is ``fetches[name]``, the host checkout
    ``.sources/<name>-<pin[:12]>``.
    """

    __slots__ = ()
    name: str
    source: Source

    @property
    def mutable(self) -> bool:
        """True when the declaration alone does not pin the source."""
        return self.source.inline_pin is None

    def pin_from(self, fetches: Mapping[str, LockedFetch]) -> str | None:
        """This source's pin: inline, or a lockfile entry recorded for the same source."""
        if self.source.inline_pin is not None:
            return self.source.inline_pin
        locked = fetches.get(self.name)
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
            source=self.source.url, kind=self.source.kind, digest=digest, name=self.name, ref=ref
        )

    def pin_dir(self, pin: str) -> str:
        """``<name>-<pin[:12]>``: the directory under ``.sources`` that holds *pin*'s checkout."""
        return f"{self.name}-{pin[:12]}"


@dataclass(frozen=True, slots=True)
class KernelSource(NamedSource):
    """The source of a built kernel (one with a config), named ``kernel`` or ``kernel-<variant>``.

    Outside ``nethermind-v1`` the kernel build script copies its host-fetched
    checkout from ``$SRCDIR/tundravm-sources/<name>-<pin[:12]>``.
    """

    name: str
    source: Source
    version: str

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
class SourceBuild(NamedSource):
    """Fetch *source*, run *build* inside mkosi-chroot, install the results.

    *install* lists the :class:`Install` steps; artifacts are cached under their
    destination's file name and installed in that order.

    ``cache_key`` overrides the default ``<name>-<url sha256[:12]>-<ref>`` build
    cache key; a lockfile pin appends ``-<pin[:12]>`` to it (default key: replaces
    the ref) so moving a pin rebuilds. ``mark_unpinned`` prefixes the unpinned hook
    with ``# unpinned: <ref>``; modules that must keep the exact hook bytes they
    emitted before source builds existed turn it off.
    """

    name: str
    source: Source
    build: BuildRecipe
    install: tuple[Install, ...] = ()
    cache_key: str | None = None
    mark_unpinned: bool = True

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

    def render(self, pin: str | None = None, *, mounted: bool = False) -> str:
        """The build-phase hook: fetch, build, cache, install.

        *pin* is a lockfile pin; an inline pin (commit ref, ``sha256=``) wins.
        *mounted* copies the host-fetched checkout ``$SRCDIR/tundravm-sources/
        <name>-<pin[:12]>`` instead of fetching in the sandbox; unpinned, the
        mounted hook only fails, naming ``tundravm lock`` and ``tundravm fetch``.
        """
        effective = self.source.inline_pin or pin
        workdir = Build.chroot_path(self.name).rel
        if isinstance(self.source, GitSource) and self.source.subdir:
            workdir = f"{workdir}/{self.source.subdir.strip('/')}"
        inner = self.build.command(f"/build/{workdir}").replace("'", "'\\''")
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
            command = f"{self._copy(effective)} && mkosi-chroot bash -c '{inner}'"
            return self._cache(effective, workdir).wrap(command, root=MOUNTED_CACHE_ROOT)
        command = f"{self._fetch(effective)} && mkosi-chroot bash -c '{inner}'"
        hook = self._cache(effective, workdir).wrap(command)
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

    def _cache(self, pin: str | None, workdir: str) -> CacheDecl:
        if self.cache_key is not None:
            key = self.cache_key if pin is None else f"{self.cache_key}-{pin[:12]}"
        else:
            url_hash = hashlib.sha256(self.source.url.encode("utf-8")).hexdigest()[:12]
            if pin is not None:
                version = pin[:12]
            elif isinstance(self.source, GitSource):
                version = self.source.ref
            else:
                version = "unpinned"
            key = f"{self.name}-{url_hash}-{version}"
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


def is_fetched(checkout: Path, pin: str) -> bool:
    """Whether *checkout* holds a complete fetch of *pin* (its marker names the pin)."""
    try:
        return (checkout / FETCH_MARKER).read_text(encoding="utf-8").strip() == pin
    except OSError:
        return False


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
        section = f"sources.{name}"
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
    removed = sorted(f"sources.{name}" for name in fetches if name not in builds)
    return added, changed, removed, details


__all__ = [
    "BuildRecipe",
    "CargoBuild",
    "DotnetBuild",
    "GitSource",
    "GoBuild",
    "HttpSource",
    "Install",
    "InstallKind",
    "KernelSource",
    "NamedSource",
    "Resolver",
    "ScriptBuild",
    "Source",
    "SourceBuild",
    "default_resolver",
    "fetch_source",
    "is_fetched",
    "resolve_pins",
    "source_drift",
]
