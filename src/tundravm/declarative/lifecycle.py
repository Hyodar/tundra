"""The lifecycle as functions over a :class:`Recipe`: compile, lint, lock, bake, measure, deploy.

Each function lowers the recipe (:func:`~tundravm.declarative.lower`) and runs
the compiler's existing step on the lowered image, so the results here are
the same bytes, lockfiles and manifests the CLI produces. Inputs and results
are explicit values: a :class:`Tree` is held in memory until written, a
:class:`Lock` is passed to :func:`bake`, and an :class:`Artifact` carries the
digests that measurement and deployment check.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import Final, Literal, get_args

from tundravm import check as _check
from tundravm._modules import SecretDelivery
from tundravm._source import (
    SOURCES_DIRNAME,
    GitSource,
    LanguageBuild,
    NamedSource,
    Resolver,
    SourceBuild,
    fetch_source,
    is_fetched,
    prefetch_deps,
    resolve_pins,
    write_marker,
)
from tundravm.backends import (
    InProcessBackend,
    LimaMkosiBackend,
    LocalLinuxBackend,
    NixMkosiBackend,
    Requirement,
)
from tundravm.backends.base import BuildBackend, is_mkosi_state
from tundravm.deploy import DeployAdapter, get_adapter
from tundravm.diff import diff_trees
from tundravm.errors import (
    ArtifactError,
    DeploymentError,
    LockfileError,
    MeasurementError,
    PolicyError,
    StateError,
    ValidationError,
)
from tundravm.lockfile import (
    LockedFetch,
    Lockfile,
    build_lockfile,
    serialize_lockfile,
)
from tundravm.measure import derive_measurements
from tundravm.models import (
    BAKE_RESULT_FILENAME,
    ArtifactRef,
    BakeResult,
    DeployRequest,
    ProfileBuildResult,
)
from tundravm.observability import Event, Progress, Reporter, TextReporter

from . import _bake, _compile
from ._lowered import LOCK_FILENAME, Lowered
from .lower import lower
from .model import Diagnostic, Git, Http, Pairs, Recipe, Target
from .resolve import lint as _resolution_lint

Scheme = Literal["rtmr", "azure", "gcp"]
BackendKind = Literal["lima", "nix", "local", "inprocess"]
ProbeRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

INPROCESS = "inprocess"
MANIFEST_KEY = "declarative"
"""Top-level ``bake-result.json`` key holding the recipe/tree digests and ``simulated``."""


def _version() -> str:
    from tundravm import __version__

    return __version__


# ── result types ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Pin:
    """A locked source: *identity* is the build name (or the URL for anonymous fetches)."""

    identity: str
    source: Git | Http
    digest: str


@dataclass(frozen=True, slots=True)
class Lock:
    """A lockfile as a value; ``sections`` maps payload sections to their digests.

    ``compiler_version`` is the tundravm version that wrote it (its ``compiler``
    section; this version for a lockfile older than version 4).
    """

    recipe_digest: str
    sections: Pairs
    pins: tuple[Pin, ...]
    compiler_version: str
    lockfile: Lockfile = field(compare=False, repr=False)

    @classmethod
    def of(cls, lockfile: Lockfile) -> Lock:
        compiler = lockfile.recipe.get("compiler")
        written = compiler.get("tundravm") if isinstance(compiler, dict) else None
        return cls(
            recipe_digest=lockfile.recipe_digest,
            sections=tuple(sorted(lockfile.sections.items())),
            pins=tuple(_pin(fetch) for fetch in lockfile.fetches),
            compiler_version=str(written) if written is not None else _version(),
            lockfile=lockfile,
        )

    def text(self) -> str:
        """The serialized lockfile, byte-identical to what ``tundravm lock`` writes."""
        return serialize_lockfile(self.lockfile)


@dataclass(frozen=True, slots=True)
class Entry:
    """One tree path: file bytes (``content``), a directory (both ``None``) or a symlink."""

    path: str
    content: bytes | None
    mode: int
    symlink: str | None = None


@dataclass(frozen=True, slots=True)
class Tree:
    """A compiled mkosi project held in memory; ``variants`` are its top-level directories.

    ``digest`` covers each path, its bytes or symlink target and, for files, the
    exec bit alone: the umask and git keep nothing more, so the same tree hashes
    the same on every host.
    """

    entries: tuple[Entry, ...]
    digest: str
    variants: tuple[str, ...] = ()

    def write(self, path: Path) -> None:
        """Write the tree to *path*, replacing its variant directories and dropping stale ones."""
        root = Path(path)
        root.mkdir(parents=True, exist_ok=True)
        tops = {entry.path.split("/", 1)[0] for entry in self.entries}
        for child in root.iterdir():
            stale = child.is_dir() and (child / "mkosi.conf").is_file()
            if child.name in tops or stale:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        for entry in self.entries:
            target = root / entry.path
            if entry.symlink is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(entry.symlink)
            elif entry.content is None:
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(entry.mode)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(entry.content)
                target.chmod(entry.mode)


@dataclass(frozen=True, slots=True)
class Backend:
    """Where :func:`bake` builds; ``inprocess`` writes simulated artifacts (tests)."""

    kind: BackendKind
    cpus: int = 2
    memory: str = "4GiB"
    disk: str = "40GiB"

    def __post_init__(self) -> None:
        if self.kind not in get_args(BackendKind):
            raise ValidationError(
                f"Unknown backend kind {self.kind!r}.",
                hint=f"Expected one of: {', '.join(get_args(BackendKind))}",
            )

    def build_backend(self) -> BuildBackend:
        """The compiler's backend instance for this kind."""
        if self.kind == "lima":
            return LimaMkosiBackend(cpus=self.cpus, memory=self.memory, disk=self.disk)
        if self.kind == "nix":
            return NixMkosiBackend()
        if self.kind == "local":
            return LocalLinuxBackend()
        return InProcessBackend()


@dataclass(frozen=True, slots=True)
class Artifact:
    path: Path
    variant: str
    target: Target
    sha256: str
    recipe_digest: str
    lock_digest: str
    tree_digest: str
    simulated: bool = False
    ports: tuple[int, ...] = ()
    """Guest ports the variant's ``Secrets`` deliveries listen on; ``deploy`` forwards them."""


@dataclass(frozen=True, slots=True)
class Measurements:
    """Expected measurement *values*; ``tool`` names what measured them."""

    scheme: Scheme
    values: Pairs
    tool: str
    artifact_digest: str

    def to_json(self, path: Path | None = None) -> str:
        """The measurements as JSON (sorted keys, trailing newline), also written to *path*."""
        payload = {
            "artifact_digest": self.artifact_digest,
            "scheme": self.scheme,
            "tool": self.tool,
            "values": dict(self.values),
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if path is not None:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return text

    def verify(self, expected: Mapping[str, str]) -> tuple[str, ...]:
        """The registers whose value differs from *expected*, sorted; empty when all match.

        A register only one side holds counts as a mismatch.
        """
        actual = dict(self.values)
        return tuple(
            name for name in sorted({*actual, *expected}) if actual.get(name) != expected.get(name)
        )


@dataclass(frozen=True, slots=True)
class Qemu:
    memory: str = "2G"
    cpus: int = 2
    ssh_port: int = 2222
    tdx: bool = False
    daemonize: bool = True
    """``False`` runs QEMU in the foreground with its console on this terminal."""
    forward: tuple[tuple[int, int], ...] = ()
    """``(host, guest)`` TCP ports forwarded besides ``ssh_port``; ``Secrets`` ports are added."""


@dataclass(frozen=True, slots=True)
class Azure:
    storage_account: str
    resource_group: str = "tdx-vms"
    location: str = "eastus"
    vm_size: str = "Standard_DC2es_v5"
    """A TDX confidential VM size: the DCesv5/DCedsv5 or ECesv5/ECedsv5 series."""
    gallery: str = "tdx_images"
    """The Compute Gallery (in ``resource_group``) the image version is published to."""
    secure_boot: bool = False
    """Create the VM with Secure Boot on; requires ``signed`` (an unsigned UKI cannot boot)."""
    signed: bool = False
    """The UKI was signed, outside tundravm, with keys Azure's Secure Boot firmware trusts."""


@dataclass(frozen=True, slots=True)
class Gcp:
    project: str
    bucket: str
    zone: str = "us-central1-a"
    machine_type: str = "c3-standard-4"
    """An Intel TDX machine type: the C3 series."""


DeployTarget = Qemu | Azure | Gcp
DEPLOY_TARGETS: dict[Target, type[Qemu] | type[Azure] | type[Gcp]] = {
    "qemu": Qemu,
    "azure": Azure,
    "gcp": Gcp,
}


@dataclass(frozen=True, slots=True)
class Deployment:
    id: str
    target: Target
    endpoint: str | None
    metadata: Pairs


# ── compile / diff ───────────────────────────────────────────────────────


def variant_names(recipe: Recipe, variants: Sequence[str] | None) -> tuple[str, ...]:
    """*variants* checked against the recipe, or every declared variant."""
    declared = tuple(v.name for v in recipe.variants)
    if variants is None:
        return declared
    unknown = [name for name in variants if name not in declared]
    if unknown:
        raise ValidationError(
            f"Unknown variant(s): {', '.join(unknown)}.",
            hint=f"Declared variants: {', '.join(declared)}",
        )
    return tuple(dict.fromkeys(variants))


@contextmanager
def using_lock(img: Lowered, locked: Lock | None) -> Iterator[Lowered]:
    """*img* reading a scratch copy of *locked* (or no lockfile at all) as its lockfile."""
    with tempfile.TemporaryDirectory(prefix="tundravm-lock-") as tmp:
        path = Path(tmp) / LOCK_FILENAME
        if locked is not None:
            path.write_text(locked.text(), encoding="utf-8")
        yield replace(img, lock_file=path)


def compile_image(img: Lowered, profiles: Sequence[str] | None, *, locked: Lock | None) -> Tree:
    """The tree *img* compiles to for *profiles*, pinned by *locked* alone."""
    with (
        tempfile.TemporaryDirectory(prefix="tundravm-tree-") as tmp,
        using_lock(img.select(profiles), locked) as pinned,
    ):
        _compile.emit(pinned, Path(tmp))
        return read_tree(Path(tmp), variants=pinned.active)


def read_tree(
    root: Path,
    *,
    variants: Sequence[str] = (),
    warn: Callable[[str], None] | None = None,
) -> Tree:
    """Load the tree at *root* into memory.

    mkosi's build state next to its config (``mkosi.tools``, ``mkosi.cache``,
    ``mkosi.builddir``, ...) is not part of the tree. An unreadable path is left
    out and reported to *warn*.
    """
    entries: list[Entry] = []

    def skip(path: Path, exc: OSError) -> None:
        if warn is not None:
            warn(f"skipped unreadable {path}: {exc.strerror or exc}")

    def walk_error(exc: OSError) -> None:
        skip(Path(exc.filename or root), exc)

    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error):
        base = Path(dirpath)
        names = [*dirnames, *filenames]
        kept = {n for n in names if not is_mkosi_state((base / n).relative_to(root).as_posix())}
        dirnames[:] = sorted(n for n in dirnames if n in kept)
        for name in sorted(kept):
            path = base / name
            rel = path.relative_to(root).as_posix()
            try:
                info = path.lstat()
                mode = info.st_mode & 0o7777
                if path.is_symlink():
                    entries.append(Entry(rel, None, mode, symlink=os.readlink(path)))
                elif path.is_dir():
                    if not any(path.iterdir()):
                        entries.append(Entry(rel, None, mode))
                else:
                    entries.append(Entry(rel, path.read_bytes(), mode))
            except OSError as exc:
                skip(path, exc)
                if name in dirnames:
                    dirnames.remove(name)
    entries.sort(key=lambda e: e.path)
    return Tree(entries=tuple(entries), digest=_tree_digest(entries), variants=tuple(variants))


def read_variant_tree(root: Path, variants: Sequence[str]) -> Tree:
    """The tree of *variants*' directories under *root*, as :func:`compile` returns it."""
    whole = read_tree(root, variants=variants)
    entries = [entry for entry in whole.entries if entry.path.split("/", 1)[0] in variants]
    return Tree(entries=tuple(entries), digest=_tree_digest(entries), variants=tuple(variants))


def _digest_mode(entry: Entry) -> int:
    """*entry*'s mode as the tree digest sees it: ``0755``/``0644`` by exec bit for files."""
    if entry.symlink is not None:
        return 0o777
    if entry.content is None:
        return 0o755
    return 0o755 if entry.mode & 0o111 else 0o644


def _tree_digest(entries: Sequence[Entry]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        mode = _digest_mode(entry)
        digest.update(entry.path.encode() + b"\0" + f"{mode:o}".encode() + b"\0")
        if entry.symlink is not None:
            digest.update(b"L" + entry.symlink.encode())
        elif entry.content is not None:
            digest.update(b"F" + hashlib.sha256(entry.content).digest())
        else:
            digest.update(b"D")
        digest.update(b"\n")
    return digest.hexdigest()


def compile(
    recipe: Recipe,
    *,
    variants: Sequence[str] | None = None,
    lock: Lock | None = None,
) -> Tree:
    """The mkosi tree for *variants* (default: all), with *lock*'s source pins applied.

    Without *lock* no lockfile is consulted, so source builds use their refs.
    """
    names = variant_names(recipe, variants)
    return compile_image(lower(recipe, variants=names), names, locked=lock)


def diff(tree: Tree, against: Tree | Path) -> str:
    """A unified diff from *against* to *tree*; empty when they match.

    Variant directories in *against* that *tree* does not hold are not compared.
    """
    with tempfile.TemporaryDirectory(prefix="tundravm-diff-") as tmp:
        new = Path(tmp) / "new"
        tree.write(new)
        if isinstance(against, Tree):
            old = Path(tmp) / "old"
            against.write(old)
        else:
            old = Path(against)
        ignore = _foreign_variant_globs(old, tree.variants)
        return diff_trees(old, new, ignore=ignore).unified()


def _foreign_variant_globs(root: Path, compiled: Sequence[str]) -> list[str]:
    from tundravm.diff import _foreign_profile_globs

    return _foreign_profile_globs(root, compiled) if compiled else []


# ── lint ─────────────────────────────────────────────────────────────────


class NoLock(Enum):
    """The type of :data:`NO_LOCK`."""

    NO_LOCK = "no lock"


NO_LOCK: Final = NoLock.NO_LOCK
"""``check_report(lock=NO_LOCK)``: no lockfile at all, never the image's default one."""


def check_report(
    recipe: Recipe | None,
    img: Lowered | None,
    *,
    variants: Sequence[str] | None,
    lock: Lock | Path | NoLock | None = None,
) -> list[_check.Diagnostic]:
    """Declarative and compiler diagnostics in the compiler's report form.

    The recipe's resolution diagnostics come first, in resolution order. When
    none is an error, the lowered image's ``check()`` findings follow, sorted.
    The compiler checks see *lock*'s source pins (a :class:`Lock`, or the
    lockfile at a path, none when it is missing; :data:`NO_LOCK`: none at
    all); without it the image reads its own lockfile (``build/tundravm.lock``).
    """
    found: list[_check.Diagnostic] = []
    names: tuple[str, ...] | None = None
    if recipe is not None:
        names = variant_names(recipe, variants)
        for d in _resolution_lint(recipe, variants=names):
            found.append(
                _check.Diagnostic(
                    level=d.level,
                    code=d.code,
                    message=d.message,
                    hint=None,
                    profile=d.variant or "common",
                    subject=d.subject or None,
                )
            )
        if any(d.level == "error" for d in found):
            return found
        img = lower(recipe, variants=names)
    assert img is not None
    profiles = names if names is not None else (None if variants is None else tuple(variants))
    with ExitStack() as stack:
        if isinstance(lock, Lock | NoLock):
            img = stack.enter_context(using_lock(img, lock if isinstance(lock, Lock) else None))
        elif lock is not None:
            img = replace(img, lock_file=Path(lock))
        checked = _check.check(img, profiles=profiles)
    checked = sorted(checked, key=lambda d: (d.profile, _LEVELS[d.level], d.code, d.subject or ""))
    found.extend(d for d in checked if d not in found)
    return found


_LEVELS = {"error": 0, "warning": 1, "info": 2}


def lint(
    recipe: Recipe,
    *,
    variants: Sequence[str] | None = None,
    lock: Lock | None = None,
) -> tuple[Diagnostic, ...]:
    """Every problem with *variants* (default: all): resolution, fragment checks, compiler rules.

    With *lock*, its source pins apply before the compiler rules run (a pinned
    source never reports ``source-unpinned``) and drift against it is reported
    too (:func:`lock_status`).
    """
    found = [
        Diagnostic(
            code=d.code,
            message=d.message,
            level=d.level,
            variant="" if d.profile == "common" else d.profile,
            subject=d.subject or "",
        )
        for d in check_report(recipe, None, variants=variants, lock=lock)
    ]
    if lock is not None and not any(d.level == "error" for d in found):
        found.extend(lock_status(recipe, lock, variants=variants))
    return tuple(dict.fromkeys(found))


# ── lock ─────────────────────────────────────────────────────────────────


def _pin(fetch: LockedFetch) -> Pin:
    identity = fetch.name or fetch.source
    if fetch.kind == "git":
        return Pin(identity, Git(fetch.source, fetch.ref or fetch.digest), fetch.digest)
    return Pin(identity, Http(fetch.source, sha256=fetch.digest), fetch.digest)


def lock_image(
    img: Lowered,
    profiles: Sequence[str] | None,
    *,
    previous: Lock | None = None,
    update: Sequence[str] = (),
    offline: bool = False,
    resolver: Resolver | None = None,
) -> Lock:
    """Lock *img*'s *profiles*, keeping *previous* pins except for the *update* sources.

    The sources are the source builds and, outside ``nethermind-v1``, the built
    kernels' sources (``kernel``, or ``kernel-<variant>``; see ``Lowered.lock_sources``).
    """
    offline = offline or img.policy.network_mode == "offline"
    scoped = img.select(profiles)
    payload = scoped.payload()
    builds = scoped.lock_sources()
    unknown = [
        name
        for name in update
        if name not in builds and not any(b.name == name for b in builds.values())
    ]
    if unknown:
        raise ValidationError(
            f"Cannot update unknown source(s): {', '.join(unknown)}.",
            hint=f"Declared sources: {', '.join(builds) or '(none)'}",
        )
    update = [key for key, build in builds.items() if key in update or build.name in update]
    prior = {} if previous is None else _named_pins(previous)
    kept: list[LockedFetch] = []
    pending: dict[str, NamedSource] = {}
    for name, build in builds.items():
        pin = None if name in update else build.pin_from(prior)
        if pin is None:
            pending[name] = build
        else:
            kept.append(build.locked(pin))
    fetches = resolve_pins(pending, prior, resolver=resolver, offline=offline, kept=kept)
    fetches.sort(key=lambda fetch: fetch.name or fetch.source)
    return Lock.of(build_lockfile(recipe=payload, fetches=fetches))


def _named_pins(locked: Lock) -> dict[str, LockedFetch]:
    return {f.name: f for f in locked.lockfile.fetches if f.name is not None}


def lock(
    recipe: Recipe,
    *,
    previous: Lock | None = None,
    update: Sequence[str] = (),
    offline: bool = False,
    resolver: Resolver | None = None,
    variants: Sequence[str] | None = None,
) -> Lock:
    """Lock every variant (or *variants*): section digests plus a pin per source build.

    Pins in *previous* that still match their source are kept; sources named in
    *update* are resolved again (a build whose source differs between variants is
    pinned per variant, as ``<variant>/<name>``: name that key, or the build's
    name for every variant's pin). *resolver* replaces the network lookup;
    ``offline=True`` fails for any source *previous* does not pin. Every source is
    attempted before failing: one :class:`~tundravm.errors.LockfileError` lists
    each source that could not be resolved, with the reason, and its ``failures``
    maps their names to :class:`~tundravm.errors.SourceError`.
    """
    names = variant_names(recipe, variants)
    return lock_image(
        lower(recipe, variants=names),
        names,
        previous=previous,
        update=update,
        offline=offline,
        resolver=resolver,
    )


def drift_diagnostics(drift_lines: Iterator[tuple[str, str, str]]) -> tuple[Diagnostic, ...]:
    return tuple(
        Diagnostic(code, message, level="error", subject=section)
        for code, section, message in drift_lines
    )


def image_lock_status(
    img: Lowered,
    locked: Lock,
    profiles: Sequence[str] | None,
    *,
    partial: bool = False,
    resolver: Resolver | None = None,
) -> tuple[Diagnostic, ...]:
    """Drift between *img*'s *profiles* and *locked*, one diagnostic per section.

    *partial* compares only the selected profiles' sections
    (see :func:`~tundravm.lockfile.compare_lock`).
    """
    drift = img.select(profiles).drift(locked.lockfile, resolver=resolver, partial=partial)
    details = drift.details

    def lines() -> Iterator[tuple[str, str, str]]:
        if drift.lock_version is not None:
            yield (
                "lock-changed",
                "version",
                f"the lockfile is version {drift.lock_version}: lock again to record the "
                "distribution, compiler and kernel sections",
            )
        for section in drift.changed:
            detail = f": {details[section]}" if section in details else ""
            yield "lock-changed", section, f"{section} changed since the lock{detail}"
        for section in drift.added:
            yield "lock-added", section, details.get(section, f"{section} is not in the lock")
        for section in drift.removed:
            yield "lock-removed", section, f"{section} is only in the lock"

    found = drift_diagnostics(lines())
    if not found and not drift.digest_matches:
        found = (Diagnostic("lock-stale", "the recipe digest differs from the lock"),)
    return found


def lock_status(
    recipe: Recipe,
    locked: Lock,
    *,
    variants: Sequence[str] | None = None,
    resolver: Resolver | None = None,
) -> tuple[Diagnostic, ...]:
    """Every section and source where *recipe* drifted from *locked*; empty when current.

    With *variants* naming only some of the recipe's variants, the lock's sections
    of the others are not compared, so a lock of every variant covers a subset.
    *resolver* (as for :func:`lock`) also reports git refs that moved since the lock.
    """
    names = variant_names(recipe, variants)
    partial = set(names) != {v.name for v in recipe.variants}
    return image_lock_status(
        lower(recipe, variants=names), locked, names, partial=partial, resolver=resolver
    )


def read_lock(path: Path) -> Lock:
    """Parse the lockfile at *path*."""
    from tundravm.lockfile import read_lockfile

    return Lock.of(read_lockfile(path))


def write_lock(locked: Lock, path: Path) -> None:
    """Write *locked* to *path* (parents created)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(locked.text(), encoding="utf-8")


# ── fetch ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FetchedSource:
    """A source's checkout at *path* (``<out>/.sources/<name>-<pin[:12]>``).

    *name* is a source build's, ``kernel`` (``kernel-<variant>``) for a built
    kernel's source, or ``efi-stub`` for an ``EfiStub`` package. *pin* is the commit
    (git) or sha256 (http) it holds; *cached* when an earlier fetch had already
    completed it.
    """

    name: str
    kind: Literal["git", "http"]
    url: str
    pin: str
    path: Path
    cached: bool = False
    ref: str | None = None
    """The git ref the pin was resolved from; ``None`` for http."""
    deps: str | None = None
    """A Go, Cargo or .NET build's dependency prefetch into ``<out>/.sources/deps``:
    ``go: prefetched``, ``go: kept``, ``skipped (no host go)``, ``skipped (offline)`` or
    ``failed (<reason>)``; ``None`` for other sources."""

    def locked(self) -> LockedFetch:
        """The lockfile entry this checkout matches."""
        return LockedFetch(
            source=self.url, kind=self.kind, digest=self.pin, name=self.name, ref=self.ref
        )


def fetch_image(
    img: Lowered,
    profiles: Sequence[str] | None,
    *,
    locked: Lock | None,
    out: Path,
    resolver: Resolver | None = None,
    reporter: Reporter | None = None,
    force: bool = False,
) -> tuple[FetchedSource, ...]:
    """Check out every source of *img*'s *profiles* under ``<out>/.sources``.

    The sources are the source builds and, outside ``nethermind-v1``, the built
    kernels' sources and ``EfiStub`` packages (``Lowered.lock_sources``). Each Go,
    Cargo and .NET build's dependencies are then prefetched into
    ``<out>/.sources/deps`` with the host's toolchain (``prefetch_deps``); without
    it the build downloads them as before. *force* also refills the dependency
    caches.

    Pins come from *locked*; sources it does not pin are resolved first (as
    :func:`lock` would, through *resolver*), which ``mutable_ref_policy="error"``
    refuses. A complete checkout is verified (``is_fetched``) and kept; one that
    changed since it was fetched raises ``SourceError`` unless *force*, which
    checks every source out again. Sources with the same checkout (the same
    source and pin, e.g. a build pinned per variant) share one.
    """
    builds = img.select(profiles).lock_sources()
    if not builds:
        return ()
    prior = {} if locked is None else _named_pins(locked)
    pins: dict[str, str] = {}
    pending: dict[str, NamedSource] = {}
    for name, build in builds.items():
        pin = build.pin_from(prior)
        if pin is None:
            pending[name] = build
        else:
            pins[name] = pin
    offline = img.policy.network_mode == "offline"
    if pending:
        pins.update(_resolve_for_fetch(img, pending, resolver=resolver, offline=offline))
    root = Path(out) / SOURCES_DIRNAME
    progress = Progress(reporter)
    fetched: list[FetchedSource] = []
    noun = "source" if len(builds) == 1 else "sources"
    copies: dict[tuple[object, str], Path] = {}
    done: set[Path] = set()
    cleared: set[str] = set()
    with progress.phase("fetch", f"fetch {len(builds)} {noun}", profile=None):
        for name, build in builds.items():
            source = _checkout(
                build,
                pins[name],
                root,
                progress,
                offline=offline,
                copies=copies,
                force=force and root / build.pin_dir(pins[name]) not in done,
            )
            copies.setdefault((build.source, source.pin), source.path)
            done.add(source.path)
            if isinstance(build, SourceBuild):
                deps = _prefetch(
                    build, source, root, progress, offline=offline, force=force, cleared=cleared
                )
                source = replace(source, deps=deps)
            fetched.append(source)
    return tuple(fetched)


def _prefetch(
    build: SourceBuild,
    source: FetchedSource,
    root: Path,
    progress: Progress,
    *,
    offline: bool,
    force: bool,
    cleared: set[str],
) -> str | None:
    """Prefetch *build*'s dependencies next to its checkout (``prefetch_deps``), with a notice.

    With *force* each dependency cache is removed before its first prefetch of
    this fetch (*cleared* names the caches already removed) and prefetched again.
    """
    cache = build.build.deps if isinstance(build.build, LanguageBuild) else None
    clear = force and cache is not None and cache not in cleared
    outcome = prefetch_deps(
        build, source.pin, source.path, root, offline=offline, force=force, clear=clear
    )
    if outcome is None or cache is None:
        return None
    cleared.add(cache)
    message = f"{build.key}: deps: {outcome}"
    if outcome.startswith("failed") or ": incomplete" in outcome:
        progress.emit("warning", None, f"{message}; the build downloads them", level="warning")
    else:
        progress.emit("log", None, message, source="notice")
    return outcome


def _resolve_for_fetch(
    img: Lowered, pending: Mapping[str, NamedSource], *, resolver: Resolver | None, offline: bool
) -> dict[str, str]:
    """Pins for the *pending* sources no lockfile pins, under the recipe's policy."""
    if img.policy.mutable_ref_policy == "error":
        names = ", ".join(f"{b.name}@{b.source.requested}" for b in pending.values())
        raise PolicyError(
            f"Unpinned source builds are not allowed by policy: {names}.",
            hint="Run `tundravm lock RECIPE` to pin them, then fetch from that lockfile.",
            context={"operation": "fetch", "sources": names},
        )
    try:
        resolved = resolve_pins(pending, {}, resolver=resolver, offline=offline)
    except LockfileError as exc:
        message = str(exc.args[0]).replace("Cannot lock", "Cannot fetch", 1)
        raise LockfileError(
            message.replace("; nothing written.", "; nothing fetched."),
            hint="Fix the sources above, or `tundravm lock RECIPE` and fetch from its lockfile.",
            failures=exc.failures,
        ) from exc
    return {fetch.name: fetch.digest for fetch in resolved if fetch.name is not None}


def _checkout(
    build: NamedSource,
    pin: str,
    root: Path,
    progress: Progress,
    *,
    offline: bool,
    copies: Mapping[tuple[object, str], Path],
    force: bool = False,
) -> FetchedSource:
    """*build*'s checkout of *pin* under *root*: kept when complete, else fetched afresh.

    A complete checkout is verified first (``is_fetched``: a changed one raises
    ``SourceError``); *force* replaces it without looking. A complete checkout
    of the same source and pin in *copies* is copied instead of fetched again
    (builds of one repository share it).
    """
    path = root / build.pin_dir(pin)
    ref = build.source.ref if isinstance(build.source, GitSource) else None
    result = FetchedSource(build.key, build.source.kind, build.source.url, pin, path, ref=ref)
    if not force and is_fetched(path, pin, build.source, name=build.key):
        progress.emit("log", None, f"{build.key}: {pin[:12]} already fetched", source="notice")
        return replace(result, cached=True)
    donor = copies.get((build.source, pin))
    if offline and donor is None:
        raise PolicyError(
            f"Cannot fetch {build.name!r}: policy network_mode is offline.",
            hint=f"Populate {path} on a connected host (`tundravm fetch`), or relax network_mode.",
            context={"operation": "fetch", "source": build.source.describe()},
        )
    root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=root))
    try:
        if donor is not None:
            shutil.copytree(donor, scratch / "tree", symlinks=True)
        else:
            fetch_source(build.source, pin, scratch / "tree")
        write_marker(scratch / "tree", build.source, pin)
        if path.exists():
            shutil.rmtree(path)  # incomplete (an interrupted fetch), or replaced by force
        (scratch / "tree").rename(path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    how = "fetched" if donor is None else f"copied from {donor.name}:"
    progress.emit(
        "log", None, f"{build.key}: {how} {build.source.describe()} at {pin[:12]}", source="notice"
    )
    return result


def fetch(
    recipe: Recipe,
    *,
    lock: Lock | None,
    out: Path,
    variants: Sequence[str] | None = None,
    resolver: Resolver | None = None,
    force: bool = False,
) -> tuple[FetchedSource, ...]:
    """Check out the sources of *variants* (default: all) under ``<out>/.sources``.

    The sources are the source builds and, outside ``nethermind-v1``, each built
    kernel's source (git ref or commit with its submodules, or the sha256-checked
    tarball), named ``kernel`` (``kernel-<variant>`` where a variant's kernel
    source differs). Each lands in ``<out>/.sources/<name>-<pin[:12]>-<id[:8]>`` (*id*
    the source's url, subdirectory and submodules), fetched
    on this host as the invoking user (git credentials and SSH agent apply) at
    *lock*'s pin. Sources *lock* does not pin (or every source, without
    *lock*) are resolved first through *resolver* (default: the network),
    which ``mutable_ref_policy="error"`` refuses. Complete checkouts are verified
    and kept, so a second fetch touches nothing: git ``HEAD`` must be the pin with
    a clean tree (and initialised submodules when asked for), an http checkout
    must match the file manifest written when it was fetched. A checkout that
    changed raises ``SourceError``; *force* checks every source out again.
    Outside ``nethermind-v1`` the bake mounts ``<out>/.sources`` into the build
    and the build hooks and kernel build script copy their checkout from it;
    :func:`bake` fetches first. Each Go, Cargo and .NET build's dependencies are
    prefetched with the host's ``go``, ``cargo`` or ``dotnet`` into
    ``<out>/.sources/deps/{go,cargo,nuget}``, which the build hooks copy and point
    the toolchain at; a host without the toolchain skips it (``FetchedSource.deps``)
    and the build downloads them, unless it bakes offline. A cache is kept only
    while every entry of its content manifest (the modules ``go.sum`` names, the
    crates of ``Cargo.lock``, the restored NuGet packages) is still there; an
    incomplete one is prefetched again, and *force* refills every cache.
    """
    names = variant_names(recipe, variants)
    return fetch_image(
        lower(recipe, variants=names), names, locked=lock, out=out, resolver=resolver, force=force
    )


# ── bake ─────────────────────────────────────────────────────────────────


class _LineStream(io.StringIO):
    """A text stream that hands every completed line to *sink*."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        super().__init__()
        self._sink = sink
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            if line.strip():
                self._sink(line)
        return len(text)

    def isatty(self) -> bool:
        return False


def bake_image(
    img: Lowered,
    profiles: Sequence[str] | None,
    *,
    locked: Lock | None,
    backend: BuildBackend | None,
    out: Path,
    reporter: Reporter | None = None,
    lock_source: Path | None = None,
    fetch: bool = True,
    resolver: Resolver | None = None,
    offline: bool = False,
) -> tuple[BakeResult, tuple[Artifact, ...]]:
    """Bake *img* into *out*, frozen against *locked*, and record the artifact manifest.

    *locked* (read from *lock_source*, when it came from a file) is written to
    ``out/tundravm.lock`` when that file is absent or identical. A different
    lockfile already there is left alone: the bake reads *lock_source* (or a
    scratch copy of *locked*) instead, and ``bake-result.json`` records which
    lockfile it used. ``None`` bakes unfrozen with whatever lockfile ``out`` holds.

    Outside ``nethermind-v1``, *fetch* first checks the source builds out under
    ``out/.sources`` (:func:`fetch_image`, at the pins the bake builds; an
    unfrozen bake builds the pins it resolves). Without *fetch* the checkouts
    must already be there, or the bake fails with ``E_STATE``. The in-process
    backend builds nothing and fetches nothing.

    *offline* bakes as ``Policy(network_mode="offline")`` does: the fetch reuses
    complete checkouts and downloads nothing, every Go, Cargo and .NET build must
    find its prefetched dependencies (else ``E_STATE`` before mkosi runs), and
    mkosi runs the build scripts with ``--with-network=no``.
    """
    if offline and img.policy.network_mode != "offline":
        img = replace(img, policy=replace(img.policy, network_mode="offline"))
    destination = Path(out)
    destination.mkdir(parents=True, exist_ok=True)
    lock_path = destination / LOCK_FILENAME
    with ExitStack() as stack:
        used = _bake_lock(locked, lock_path, lock_source, stack)
        baking = replace(
            img.select(profiles),
            build_dir=destination,
            lock_file=None if used == lock_path else used,
            backend=img.backend if backend is None else backend,
        )
        simulated = baking.backend is not None and baking.backend.name == INPROCESS
        if fetch and baking.fetches_sources and not simulated:
            pinned = locked if locked is not None else _lock_at(lock_path)
            fetched = fetch_image(
                baking, None, locked=pinned, out=destination, resolver=resolver, reporter=reporter
            )
            baking = replace(baking, fetched_pins={s.name: s.locked() for s in fetched})
        result = _bake.bake(baking, destination, frozen=locked is not None, reporter=reporter)
    tree = read_tree(destination / "mkosi", warn=_tree_warning(reporter))
    recipe_digest = (
        locked.recipe_digest
        if locked is not None
        else (read_lock(lock_path).recipe_digest if lock_path.is_file() else "")
    )
    manifest = destination / BAKE_RESULT_FILENAME
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[MANIFEST_KEY] = {
        "recipe_digest": recipe_digest,
        "simulated": simulated,
        "tree_digest": tree.digest,
    }
    ports = {name: secret_ports(baking, name) for name in sorted(result.profiles)}
    if any(ports.values()):
        payload[MANIFEST_KEY]["ports"] = {name: list(p) for name, p in ports.items() if p}
    if locked is not None:
        source = lock_source if lock_source is not None else lock_path
        # None: an in-memory Lock read through a scratch copy
        payload[MANIFEST_KEY]["lockfile"] = (
            str(source) if lock_source is not None or used == lock_path else None
        )
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result, read_artifacts(manifest)


def secret_ports(img: Lowered, variant: str) -> tuple[int, ...]:
    """The guest ports *variant*'s ``Secrets`` deliveries listen on, sorted."""
    modules = img.applied_modules(variant, inherited=True)
    return tuple(sorted({m.port for m in modules if isinstance(m, SecretDelivery)}))


def _lock_at(path: Path) -> Lock | None:
    """The lockfile at *path*, or ``None`` when there is none."""
    return read_lock(path) if path.is_file() else None


def _tree_warning(reporter: Reporter | None) -> Callable[[str], None] | None:
    if reporter is None:
        return None

    def warn(message: str) -> None:
        reporter.emit(Event("warning", None, message, 0.0, {"level": "warning"}))

    return warn


def _bake_lock(
    locked: Lock | None, lock_path: Path, lock_source: Path | None, stack: ExitStack
) -> Path:
    """The lockfile a bake into ``lock_path.parent`` reads; never replaces a different one."""
    if locked is None:
        return lock_path
    text = locked.text()
    if not lock_path.is_file() or lock_path.read_text(encoding="utf-8") == text:
        write_lock(locked, lock_path)
        return lock_path
    if lock_source is not None and lock_source.is_file():
        if lock_source.read_text(encoding="utf-8") == text:
            return lock_source
    scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="tundravm-lock-")))
    write_lock(locked, scratch / LOCK_FILENAME)
    return scratch / LOCK_FILENAME


def bake(
    recipe: Recipe,
    *,
    lock: Lock,
    backend: Backend,
    out: Path,
    variants: Sequence[str] | None = None,
    progress: Callable[[str], None] | None = None,
    fetch: bool = True,
    offline: bool = False,
) -> tuple[Artifact, ...]:
    """Build *variants* (default: all) with *backend* into *out*, frozen against *lock*.

    Fails before building when the recipe has error diagnostics or drifted
    from *lock*. Outside ``nethermind-v1`` the source builds are fetched
    first (:func:`fetch`); ``fetch=False`` builds from the checkouts already in
    ``out/.sources`` and fails with ``E_STATE`` when one is missing.
    ``offline=True`` (or ``Policy(network_mode="offline")``) gives the build
    sandbox no network: :func:`fetch` must have checked out every source and
    prefetched every Go, Cargo and .NET build's dependencies, else ``E_STATE``.
    *progress* receives the CLI's progress lines. Writes
    ``out/bake-result.json``, which :func:`read_artifacts` reads back.
    In-process artifacts are ``simulated``.
    """
    names = variant_names(recipe, variants)
    reporter = None if progress is None else TextReporter(_LineStream(progress), live=False)
    try:
        _, artifacts = bake_image(
            lower(recipe, variants=names),
            names,
            locked=lock,
            backend=backend.build_backend(),
            out=out,
            reporter=reporter,
            fetch=fetch,
            offline=offline,
        )
    finally:
        if reporter is not None:
            reporter.close()
    return artifacts


def read_artifacts(manifest: Path) -> tuple[Artifact, ...]:
    """The artifacts recorded in *manifest* (``bake-result.json`` or the directory holding it)."""
    path = Path(manifest)
    base = path if path.is_dir() else path.parent
    result = BakeResult.load(base)
    try:
        payload = json.loads((base / BAKE_RESULT_FILENAME).read_text(encoding="utf-8"))
        extra = payload.get(MANIFEST_KEY) or {}
    except (OSError, ValueError, AttributeError) as exc:
        raise StateError(
            f"Unreadable bake result: {exc}",
            hint="Run `tundravm bake RECIPE` again to rewrite bake-result.json.",
            context={"path": str(path)},
        ) from exc
    simulated = bool(extra.get("simulated", result.backend == INPROCESS))
    ports = extra.get("ports") or {}
    artifacts: list[Artifact] = []
    for variant, profile in sorted(result.profiles.items()):
        for target, ref in sorted(profile.artifacts.items()):
            artifacts.append(
                Artifact(
                    path=Path(ref.path),
                    variant=variant,
                    target=target,
                    sha256=ref.digest or "",
                    recipe_digest=str(extra.get("recipe_digest", "")),
                    lock_digest=result.lock_digest or "",
                    tree_digest=str(extra.get("tree_digest", "")),
                    simulated=simulated,
                    ports=tuple(int(port) for port in ports.get(variant, ())),
                )
            )
    return tuple(artifacts)


def select_artifact(
    artifacts: Sequence[Artifact], *, variant: str, target: Target | None = None
) -> Artifact:
    """The artifact of *variant* (for *target*, else its only or ``qemu`` one)."""
    found = [a for a in artifacts if a.variant == variant]
    if target is not None:
        found = [a for a in found if a.target == target]
    elif len(found) > 1:
        found = [a for a in found if a.target == "qemu"] or found[:1]
    if not found:
        known = ", ".join(sorted({f"{a.variant}/{a.target}" for a in artifacts})) or "(none)"
        wanted = variant if target is None else f"{variant}/{target}"
        raise StateError(
            f"No baked artifact for {wanted}.",
            hint=f"Baked: {known}. Bake that variant and target first.",
        )
    return found[0]


# ── measure / deploy ─────────────────────────────────────────────────────


def verify_artifact(artifact: Artifact) -> None:
    """Raise ``ArtifactError`` unless *artifact*'s file still hashes to its recorded sha256."""
    context = {"variant": artifact.variant, "target": artifact.target, "path": str(artifact.path)}
    rebake = "Bake the variant again to record the artifact it should be."
    if not artifact.sha256:
        raise ArtifactError(
            f"No sha256 recorded for {artifact.path}.",
            hint=rebake,
            context=context,
        )
    try:
        with artifact.path.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise ArtifactError(
            f"Cannot read artifact {artifact.path}: {exc.strerror or exc}.",
            hint=rebake,
            context=context,
        ) from exc
    if actual != artifact.sha256:
        raise ArtifactError(
            f"Artifact {artifact.path} changed since the bake.",
            hint=f"bake-result.json records sha256 {artifact.sha256[:12]}; {rebake}",
            context={**context, "recorded": artifact.sha256, "actual": actual},
        )


def measure(
    artifact: Artifact, *, scheme: Scheme = "rtmr", allow_placeholder: bool = False
) -> Measurements:
    """Expected measurements of *artifact* from ``measured-boot``/``dstack-mr``.

    The artifact's bytes are checked against their recorded sha256 first
    (``verify_artifact``). Without a tool this raises ``MeasurementError``;
    *allow_placeholder* returns digest-derived values instead
    (``tool="placeholder"``). Simulated artifacts are refused unless
    *allow_placeholder*.
    """
    verify_artifact(artifact)
    if artifact.simulated and not allow_placeholder:
        raise MeasurementError(
            "Refusing to measure a simulated artifact.",
            hint="Bake with a real backend, or pass allow_placeholder=True for test values.",
            context={"variant": artifact.variant, "path": str(artifact.path)},
        )
    target = artifact.target
    profile = ProfileBuildResult(
        profile=artifact.variant,
        artifacts={target: ArtifactRef(target=target, path=artifact.path, digest=artifact.sha256)},
    )
    found = derive_measurements(
        backend=scheme,
        profile=artifact.variant,
        profile_result=profile,
        allow_placeholder=allow_placeholder,
    )
    tool = str(found.source)
    if found.tool_version is not None:
        tool += f" {found.tool_version}"
    return Measurements(
        scheme=scheme,
        values=tuple(sorted(found.values.items())),
        tool=tool,
        artifact_digest=artifact.sha256,
    )


def deploy_parameters(using: DeployTarget) -> dict[str, str]:
    """*using*'s fields as the adapter's string parameters (``forward`` as ``HOST:GUEST,...``)."""
    params: dict[str, str] = {}
    for item in fields(using):
        value = getattr(using, item.name)
        if isinstance(value, bool):
            params[item.name] = str(value).lower()
        elif isinstance(value, tuple):
            params[item.name] = ",".join(f"{host}:{guest}" for host, guest in value)
        else:
            params[item.name] = str(value)
    return params


def with_secret_ports(using: DeployTarget, ports: Sequence[int]) -> DeployTarget:
    """*using* forwarding every guest port in *ports* it does not forward yet, host port = guest."""
    if not isinstance(using, Qemu):
        return using
    taken = {guest for _, guest in using.forward}
    added = tuple((port, port) for port in ports if port not in taken)
    return replace(using, forward=using.forward + added) if added else using


def target_of(using: DeployTarget) -> Target:
    return next(name for name, kind in DEPLOY_TARGETS.items() if isinstance(using, kind))


def deploy(
    artifact: Artifact,
    *,
    using: DeployTarget,
    allow_simulated: bool = False,
    adapter: DeployAdapter | None = None,
) -> Deployment:
    """Deploy *artifact* with *using*'s settings; its target must match the artifact's.

    The artifact's bytes are checked against their recorded sha256 first
    (``verify_artifact``). Simulated artifacts are refused unless
    *allow_simulated*. *adapter* replaces the target's default adapter (tests).
    On QEMU every port in ``artifact.ports`` that ``using.forward`` does not
    forward is forwarded from the same host port.
    """
    verify_artifact(artifact)
    target = target_of(using)
    if artifact.simulated and not allow_simulated:
        raise DeploymentError(
            "Refusing to deploy a simulated artifact.",
            hint="Bake with a real backend first, or pass allow_simulated=True for a test run.",
            context={"variant": artifact.variant, "path": str(artifact.path)},
        )
    if artifact.target != target:
        raise DeploymentError(
            f"Cannot deploy a {artifact.target} artifact to {target}.",
            hint=f"Deploy the variant's {target} artifact.",
            context={"variant": artifact.variant},
        )
    request = DeployRequest(
        profile=artifact.variant,
        target=target,
        artifact_path=artifact.path,
        parameters=deploy_parameters(with_secret_ports(using, artifact.ports)),
    )
    result = (adapter if adapter is not None else get_adapter(target)).deploy(request)
    return Deployment(
        id=result.deployment_id,
        target=target,
        endpoint=result.endpoint,
        metadata=tuple(sorted(result.metadata.items())),
    )


# ── doctor ───────────────────────────────────────────────────────────────


def run_probe(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run *argv* with a 10s timeout, capturing output."""
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=10, check=False)


def probe(requirement: Requirement, runner: ProbeRunner) -> tuple[bool, str]:
    """``(ok, report line)`` for one host tool."""
    try:
        result: subprocess.CompletedProcess[str] | None = runner(requirement.probe)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is None or result.returncode != 0:
        label = "missing (optional)" if requirement.optional else "missing"
        return False, f"{label} {requirement.tool} — {requirement.hint}"
    text = (result.stdout or result.stderr or "").strip()
    version = text.splitlines()[0] if text else ""
    return True, f"ok {requirement.tool} {version}".rstrip()


def requirements_of(backend: BuildBackend) -> tuple[Requirement, ...]:
    requirements = getattr(backend, "requirements", None)
    return tuple(requirements()) if callable(requirements) else ()


def doctor(backend: Backend, *, runner: ProbeRunner | None = None) -> tuple[Diagnostic, ...]:
    """One diagnostic per host tool *backend* needs that is missing (warning when optional)."""
    run = runner if runner is not None else run_probe
    found: list[Diagnostic] = []
    for requirement in requirements_of(backend.build_backend()):
        ok, _ = probe(requirement, run)
        if not ok:
            found.append(
                Diagnostic(
                    "tool-missing",
                    f"{requirement.tool} is missing: {requirement.hint}",
                    level="warning" if requirement.optional else "error",
                    subject=requirement.tool,
                )
            )
    return tuple(found)


__all__ = [
    "Artifact",
    "Azure",
    "Backend",
    "BackendKind",
    "Deployment",
    "Entry",
    "FetchedSource",
    "Gcp",
    "Lock",
    "Measurements",
    "Pin",
    "Qemu",
    "Scheme",
    "Tree",
    "bake",
    "compile",
    "deploy",
    "diff",
    "doctor",
    "fetch",
    "lint",
    "lock",
    "lock_status",
    "measure",
    "read_artifacts",
    "read_lock",
    "read_tree",
    "verify_artifact",
    "write_lock",
]
