"""Source builds: a fetched source plus a build recipe, pinned through the lockfile.

Declare one with :meth:`tundravm.Image.build_from`::

    img.build_from(
        SourceBuild(
            name="tdxs",
            source=GitSource("https://github.com/Hyodar/tundra-tools.git", "master"),
            build=GoBuild(package="./cmd/tdxs", output="tdxs"),
            install=(Install.artifact("/usr/bin/tdxs"),),
        )
    )

``install=`` lists what lands in the image, in order: :meth:`Install.artifact` is
the recipe's own output, :meth:`Install.file` and :meth:`Install.tree` copy further
paths of the source tree.

The build hook clones the symbolic ref until ``Image.lock()`` resolves it to a
commit (``LockedFetch`` entries in ``tundravm.lock``); from then on the emitted
hook fetches exactly that commit and the cache key carries it.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import shlex
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from typing import Literal

from .build_cache import Build, Cache, CacheDecl, CacheDir, CacheFile
from .errors import LockfileError, ValidationError
from .lockfile.model import LockedFetch

COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PLAIN_ENV_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]*$")
_ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar.zst")


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
    """Run *script* in the source tree; *output* is the produced file, relative to it."""

    script: str
    output: str
    packages: tuple[str, ...] = ()

    kind: Literal["script"] = field(default="script", init=False, repr=False)

    @property
    def artifact(self) -> str:
        return self.output

    def command(self, workdir: str) -> str:
        return f"cd {workdir} && {self.script}"


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
                    context={"path": path},
                )
        if not self.dest.startswith("/"):
            raise ValidationError(
                f"install destination {self.dest!r} must be absolute.",
                context={"path": self.path or "", "dest": self.dest},
            )


@dataclass(frozen=True, slots=True)
class SourceBuild:
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
            raise ValidationError(f"source build {self.name!r}: GitSource requires a ref.")
        if isinstance(self.source, HttpSource) and self.source.sha256 is not None:
            if not SHA256_PATTERN.fullmatch(self.source.sha256):
                raise ValidationError(
                    f"source build {self.name!r}: sha256 must be 64 lowercase hex chars."
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

    def render(self, pin: str | None = None) -> str:
        """The build-phase hook: fetch, build, cache, install.

        *pin* is a lockfile pin; an inline pin (commit ref, ``sha256=``) wins.
        """
        effective = self.source.inline_pin or pin
        workdir = Build.chroot_path(self.name).rel
        if isinstance(self.source, GitSource) and self.source.subdir:
            workdir = f"{workdir}/{self.source.subdir.strip('/')}"
        inner = self.build.command(f"/build/{workdir}").replace("'", "'\\''")
        command = f"{self._fetch(effective)} && mkosi-chroot bash -c '{inner}'"
        hook = self._cache(effective, workdir).wrap(command)
        if effective is None and self.mark_unpinned:
            return f"# unpinned: {self.source.requested}\n{hook}"
        return hook

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


def default_resolver(source: Source) -> str:
    """Resolve *source* over the network: ``git ls-remote`` or a one-off download."""
    if source.inline_pin is not None:
        return source.inline_pin
    if isinstance(source, GitSource):
        from .fetch.git import _resolve_commit

        return _resolve_commit(repo=source.url, ref=source.ref)
    from .fetch.http import fetch
    from .policy import Policy

    with tempfile.TemporaryDirectory(prefix="tundravm-lock-") as tmp:
        path = fetch(source.url, sha256="", cache_dir=tmp, policy=Policy(require_integrity=False))
        return path.name


def resolve_pins(
    builds: Mapping[str, SourceBuild],
    previous: Mapping[str, LockedFetch],
    *,
    resolver: Resolver | None = None,
    offline: bool = False,
) -> list[LockedFetch]:
    """Lockfile entries for *builds*.

    Online, every mutable source is resolved again through *resolver* (default:
    :func:`default_resolver`). Offline, inline pins and matching *previous*
    entries are reused and anything else raises :class:`LockfileError`.
    """
    resolve = resolver or default_resolver
    pins: list[LockedFetch] = []
    needs_network: list[str] = []
    for name, build in sorted(builds.items()):
        if build.source.inline_pin is not None:
            pins.append(build.locked(build.source.inline_pin))
            continue
        if offline:
            pin = build.pin_from(previous)
            if pin is None:
                needs_network.append(name)
            else:
                pins.append(build.locked(pin))
            continue
        pins.append(build.locked(resolve(build.source)))
    if needs_network:
        raise LockfileError(
            f"Cannot lock offline: {len(needs_network)} source(s) need the network to "
            f"resolve: {', '.join(needs_network)}.",
            hint=(
                "Run `tundravm lock RECIPE` without --offline, or pin the source inline "
                "(a 40-hex commit ref, or HttpSource(sha256=...))."
            ),
            context={"sources": ", ".join(needs_network)},
        )
    return pins


def source_drift(
    builds: Mapping[str, SourceBuild],
    fetches: Mapping[str, LockedFetch],
    *,
    resolver: Resolver | None = None,
) -> tuple[list[str], list[str], list[str], dict[str, str]]:
    """``(added, changed, removed, details)`` for the ``sources.<name>`` drift lines.

    A declared source without a lockfile entry is added; one whose entry was
    resolved from a different url/ref, or (with *resolver*) whose ref now
    resolves elsewhere, is changed, detailed as ``<old sha7> -> <new sha7>``.
    """
    added: list[str] = []
    changed: list[str] = []
    details: dict[str, str] = {}
    for name, build in sorted(builds.items()):
        section = f"sources.{name}"
        locked = fetches.get(name)
        if locked is None:
            added.append(section)
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


def short_pin(pin: str | None) -> str:
    return pin[:7] if pin else "-"


__all__ = [
    "BuildRecipe",
    "CargoBuild",
    "DotnetBuild",
    "GitSource",
    "GoBuild",
    "HttpSource",
    "Install",
    "InstallKind",
    "Resolver",
    "ScriptBuild",
    "Source",
    "SourceBuild",
    "default_resolver",
    "resolve_pins",
    "source_drift",
]
