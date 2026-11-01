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
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Final, Literal, get_args

from tundravm import check as _check
from tundravm._reporting import Event, Progress, Reporter, TextReporter
from tundravm._source import (
    SOURCES_DIRNAME,
    GitSource,
    LanguageBuild,
    NamedSource,
    Resolver,
    SourceBuild,
    checkout_rust_toolchain,
    fetch_source,
    is_fetched,
    prefetch_deps,
    require_host_go,
    require_host_rustup,
    resolve_pins,
    write_marker,
)
from tundravm.backends import (
    LimaMkosiBackend,
    LocalLinuxBackend,
    Requirement,
)
from tundravm.backends.base import BuildBackend, is_mkosi_state
from tundravm.errors import ArtifactError, LockfileError, PolicyError, ValidationError
from tundravm.lockfile import (
    LockedFetch,
    Lockfile,
    build_lockfile,
    lock_variants,
    section_profile,
    serialize_lockfile,
)
from tundravm.measure import derive_measurements
from tundravm.measure.policy import policy_payload, single_values
from tundravm.models import (
    ArtifactRef,
    BakeResult,
    ProfileBuildResult,
)
from tundravm.toolchain import TOOLCHAIN_UPDATE, lock_toolchain

from . import _bake, _compile
from ._lowered import LOCK_FILENAME, Lowered
from .lower import lower
from .model import Diagnostic, Git, Http, Pairs, Recipe, Target
from .resolve import lint as _resolution_lint
from .resolve import locate, resolve

Scheme = Literal["qemu", "gcp", "azure", "portable"]
BackendKind = Literal["lima", "local"]
ProbeRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


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
    """A compiled mkosi project held in memory; ``variants`` are the variants it builds.

    ``digest`` covers each path, its bytes or symlink target and, for files, the
    exec bit alone: the umask and git keep nothing more, so the same tree hashes
    the same on every host.
    """

    entries: tuple[Entry, ...]
    digest: str
    variants: tuple[str, ...] = ()

    def write(self, path: Path) -> None:
        """Write the tree to *path*, replacing what it holds and dropping stale mkosi
        directories (a directory holding an ``mkosi.conf`` the tree does not write)."""
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
    """Where :func:`bake` builds: ``local`` (this Linux x86-64 host) or ``lima``."""

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
        return LocalLinuxBackend()


@dataclass(frozen=True, slots=True)
class Artifact:
    path: Path
    variant: str
    target: Target
    sha256: str
    recipe_digest: str
    lock_digest: str
    tree_digest: str
    lock_sha256: str | None = None
    """The sha256 of the lockfile the bake read; ``None`` when it read none."""
    manifest_sha256: str | None = None
    """The sha256 of mkosi's package manifest beside the artifact at bake time; ``None``
    when there was none."""
    build: Path | None = None
    """The build directory the bake ran in."""
    boot_payload: Path | None = None
    """The UKI this disk artifact boots, as the bake recorded it; ``None`` when unknown."""
    boot_payload_sha256: str | None = None
    """The sha256 of ``boot_payload`` at bake time."""


@dataclass(frozen=True, slots=True)
class Measurements:
    """Expected measurements; ``tool`` names what measured them.

    ``registers`` holds each register's accepted values (a hex string, or a dict with
    ``value`` and ``firmware``/``label``); ``values`` the registers with one accepted
    value. ``unverifiable`` names the registers the scheme cannot predict, with the
    reason; ``extra`` the scheme's other policy fields (``sources``, ``vtpm_pcrs``).
    ``artifact_path`` is the measured file (:func:`measure` sets it), which the
    verifier policy records next to ``artifact_digest``. ``boot_payload_path`` and
    ``boot_payload_digest`` name the UKI the measurement is of (the artifact itself
    for a UKI, the recorded boot payload for a disk), empty when unknown.
    """

    scheme: Scheme
    values: Pairs
    tool: str
    artifact_digest: str
    artifact_path: str = ""
    boot_payload_path: str = ""
    boot_payload_digest: str = ""
    registers: tuple[tuple[str, tuple[str | Mapping[str, str], ...]], ...] = ()
    unverifiable: Pairs = ()
    extra: tuple[tuple[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        """The measurements as JSON values: artifact, artifact_digest, boot_payload, scheme,
        tool, values."""
        return {
            "artifact": self.artifact_path,
            "artifact_digest": self.artifact_digest,
            "boot_payload": self._boot_payload(),
            "scheme": self.scheme,
            "tool": self.tool,
            "values": dict(self.values),
            "registers": {name: list(entries) for name, entries in self.registers},
            "unverifiable": dict(self.unverifiable),
            **dict(self.extra),
        }

    def to_policy(
        self, *, platform_approval: Mapping[str, Sequence[str]] | None = None
    ) -> dict[str, object]:
        """The verifier policy ``measure --export-policy`` writes and ``Tdxs.from_policy`` reads.

        *platform_approval* maps
        ``MRTD``/``RTMR0`` to values the operator approves for the platform (``measure
        --approve-mrtd/--approve-rtmr0``); ``attest`` trusts a quote only when both match.
        """
        return policy_payload(
            scheme=self.scheme,
            tool=self.tool,
            registers={name: list(entries) for name, entries in self.registers},
            unverifiable=dict(self.unverifiable),
            extra=dict(self.extra),
            artifact_path=self.artifact_path,
            artifact_sha256=self.artifact_digest,
            boot_payload=self._boot_payload(),
            platform_approval=platform_approval,
        )

    def _boot_payload(self) -> dict[str, str] | None:
        if not self.boot_payload_digest:
            return None
        return {"path": self.boot_payload_path, "sha256": self.boot_payload_digest}

    def verify(self, expected: Mapping[str, str]) -> tuple[str, ...]:
        """The registers whose value differs from *expected*, sorted; empty when all match.

        A register only one side holds counts as a mismatch.
        """
        actual = dict(self.values)
        return tuple(
            name for name in sorted({*actual, *expected}) if actual.get(name) != expected.get(name)
        )


# ── compile ──────────────────────────────────────────────────────────────


def variant_names(recipe: Recipe, variants: Sequence[str] | None) -> tuple[str, ...]:
    """*variants* checked against the recipe, or every declared variant."""
    declared = tuple(v.name for v in recipe.variants)
    if variants is None:
        return declared
    unknown = [name for name in variants if name not in declared]
    if unknown:
        raise ValidationError(
            f"Unknown profile(s): {', '.join(unknown)}.",
            hint=f"Declared profiles: {', '.join(declared)}",
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
    return Tree(
        entries=tuple(entries),
        digest=_tree_digest(entries),
        variants=tuple(variants),
    )


def read_variant_tree(
    root: Path,
    variants: Sequence[str],
    *,
    warn: Callable[[str], None] | None = None,
) -> Tree:
    """The tree of *variants* under *root*, as :func:`compile` returns it; another variant's
    profile overlay a previous bake left there is not part of it."""
    whole = read_tree(root, variants=variants, warn=warn)
    owners = {"", *variants}
    entries = [e for e in whole.entries if _layer_of(e.path) in owners]
    return replace(whole, entries=tuple(entries), digest=_tree_digest(entries))


PROFILE_DIRECTORIES = ("payload/mkosi.profiles/", "mkosi.profiles/")
"""Where a variant's own layers live: its payload overlay and its build-sandbox additions."""


def _layer_of(path: str) -> str | None:
    """The variant tree *path* belongs to: ``""`` for what every variant builds, the variant
    of ``payload/mkosi.profiles/<variant>/`` and ``mkosi.profiles/<variant>/``, and ``None``
    for those two directories themselves."""
    for prefix in PROFILE_DIRECTORIES:
        if f"{path}/" == prefix:
            return None
        if path.startswith(prefix):
            return path.removeprefix(prefix).partition("/")[0]
    return ""


def variant_layers(tree: Tree, variant: str) -> tuple[dict[str, Entry], ...]:
    """*variant*'s payload in *tree* as mkosi reads it, by path relative to each layer: the
    root (``mkosi.conf``, the settings every image shares), ``payload/`` and the variant's
    ``payload/mkosi.profiles/<variant>/`` over it."""
    own = f"payload/mkosi.profiles/{variant}/"
    root = {
        e.path: e
        for e in tree.entries
        if not e.path.startswith(("payload/", "mkosi.images/", "mkosi.profiles/"))
    }
    payload = {
        e.path.removeprefix("payload/"): e
        for e in tree.entries
        if e.path.startswith("payload/") and _layer_of(e.path) == ""
    }
    overlay = {e.path.removeprefix(own): e for e in tree.entries if e.path.startswith(own)}
    return (root, payload, overlay)


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
) -> list[Diagnostic]:
    """Declarative and compiler diagnostics, as ``tundravm check`` reports them.

    The recipe's resolution diagnostics come first, in resolution order. When
    none is an error, the lowered image's ``check()`` findings follow, sorted.
    The compiler checks see *lock*'s source pins (a :class:`Lock`, or the
    lockfile at a path, none when it is missing; :data:`NO_LOCK`: none at
    all); without it the image reads its own lockfile (``build/tundravm.lock``).
    """
    found: list[Diagnostic] = []
    names: tuple[str, ...] | None = None
    if recipe is not None:
        names = variant_names(recipe, variants)
        found.extend(_resolution_lint(recipe, variants=names))
        # the tree's shared payload is what every variant builds: lowering resolves them all
        seen = {(d.code, d.subject, d.location) for d in found}
        for d in _resolution_lint(recipe):
            if d.severity == "error" and (d.code, d.subject, d.location) not in seen:
                seen.add((d.code, d.subject, d.location))
                found.append(d)
        if any(d.severity == "error" for d in found):
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
    if recipe is not None:
        checked = [
            *_located(recipe, checked),
            *_check.declaration_check(recipe, variants=names),
            *_check.unit_declaration_check(recipe, variants=names),
        ]
    checked = sorted(checked, key=lambda d: (d.variant, _SEVERITY[d.severity], d.code, d.subject))
    found.extend(d for d in checked if d not in found)
    return found


_SEVERITY = {"error": 0, "warning": 1, "info": 2}


def _located(recipe: Recipe, diagnostics: Sequence[Diagnostic]) -> list[Diagnostic]:
    """*diagnostics* pointed at the declarations their subjects name in their variants."""
    declared = {v.name for v in recipe.variants}
    out: list[Diagnostic] = []
    for name in dict.fromkeys(d.variant for d in diagnostics):
        group = [d for d in diagnostics if d.variant == name]
        if name in declared and any(not d.location and d.subject for d in group):
            group = locate(group, resolve(recipe, variant=name).items)
        out.extend(group)
    return out


def lint(
    recipe: Recipe,
    *,
    variants: Sequence[str] | None = None,
    lock: Lock | None = None,
) -> tuple[Diagnostic, ...]:
    """Every problem with *variants* (default: all): resolution, fragment checks, compiler rules.

    The same :class:`Diagnostic` values ``tundravm check`` reports, hints included.
    With *lock*, its source pins apply before the compiler rules run (a pinned
    source never reports ``source-unpinned``) and drift against it is reported
    too (:func:`lock_status`).
    """
    found = check_report(recipe, None, variants=variants, lock=lock)
    if lock is not None and not any(d.severity == "error" for d in found):
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

    The sources are the source builds and the built kernel's source (``kernel``; see
    ``Lowered.lock_sources``).
    The build toolchain is recorded too (:func:`~tundravm.toolchain.lock_toolchain`);
    naming ``toolchain`` in *update* records it again instead of keeping *previous*'s.
    """
    offline = offline or img.policy.network_mode == "offline"
    scoped = img.select(profiles)
    payload = scoped.payload()
    builds = scoped.lock_sources()
    toolchain = lock_toolchain(
        img.backend,
        previous=None if previous is None else previous.lockfile.toolchain,
        update=TOOLCHAIN_UPDATE in update,
        tools_tree=img.tools_tree,
    )
    update = [name for name in update if name != TOOLCHAIN_UPDATE]
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
    return Lock.of(build_lockfile(recipe=payload, fetches=fetches, toolchain=toolchain))


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
    """Lock every variant (or *variants*): section digests, a pin per source build, the toolchain.

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


def image_lock_status(
    img: Lowered,
    locked: Lock,
    profiles: Sequence[str] | None,
    *,
    partial: bool = False,
    resolver: Resolver | None = None,
) -> tuple[Diagnostic, ...]:
    """Drift between *img*'s *profiles* and *locked*, one diagnostic per section.

    A section one profile owns reports under that profile, its subject the section's
    key there (``files``, ``sources.<name>``; see
    :func:`~tundravm.lockfile.section_profile`). *partial* compares only the selected
    profiles' sections (see :func:`~tundravm.lockfile.compare_lock`).
    """
    drift = img.select(profiles).drift(locked.lockfile, resolver=resolver, partial=partial)
    details = drift.details
    known = (*img.state.profiles, *lock_variants(locked.lockfile))

    def lines() -> Iterator[tuple[str, str, str]]:
        for section in drift.changed:
            yield "lock-changed", section, "changed since the lock"
        for section in drift.added:
            yield "lock-added", section, "is not in the lock"
        for section in drift.removed:
            yield "lock-removed", section, "is only in the lock"

    found: list[Diagnostic] = []
    for code, section, words in lines():
        profile, subject = section_profile(section, known) or ("", section)
        message = f"{subject} {words}"
        detail = details.get(section)
        if detail and code == "lock-changed":
            message += f": {detail}"
        elif detail:
            message = detail
        found.append(
            Diagnostic(code, "error", message, subject=subject, variant=profile, hint=_DRIFT_HINT)
        )
    if not found and not drift.digest_matches:
        found.append(
            Diagnostic(
                "lock-stale",
                "error",
                "the image digest differs from the lock",
                hint="Run `tundravm lock` to record it and commit the lockfile.",
            )
        )
    return tuple(found)


_DRIFT_HINT = "Review the change, then run `tundravm lock` to record it and commit the lockfile."


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
    """The lockfile at *path* (``build/tundravm.lock``), as ``tundravm.compile(image, lock=)``
    and ``plan(image, lock=)`` take it."""
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
    """A source's checkout at *path* (``<build>/.sources/<name>-<pin[:12]>``).

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
    """A Go, Cargo or .NET build's dependency prefetch into ``<build>/.sources/deps``:
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
    build: Path,
    resolver: Resolver | None = None,
    reporter: Reporter | None = None,
) -> tuple[FetchedSource, ...]:
    """Check out every source of *img*'s *profiles* under ``<build>/.sources``.

    The sources are the source builds, the built kernel's source and the ``EfiStub``
    packages (``Lowered.lock_sources``). Once every source is checked out, each Go,
    Cargo and .NET build's dependencies are prefetched into ``<build>/.sources/deps``
    with the host's toolchain (``prefetch_deps``); without it the build downloads them
    as before. A Cargo source that pins a Rust toolchain needs the host's ``rustup``,
    which ``require_host_rustup`` checks before any toolchain runs.

    Pins come from *locked*; sources it does not pin are resolved first (as
    :func:`lock` would, through *resolver*), which ``mutable_ref_policy="error"``
    refuses. A complete checkout is verified (``is_fetched``) and kept; one that
    changed since it was fetched raises ``SourceError`` (``build --clean-cache``
    fetches again). Sources with the same checkout (the same source and pin, e.g. a
    build pinned per variant) share one.
    """
    builds = img.select(profiles).lock_sources()
    if not builds:
        return ()
    prior = {} if locked is None else _named_pins(locked)
    pins: dict[str, str] = {}
    pending: dict[str, NamedSource] = {}
    for name, source_build in builds.items():
        pin = source_build.pin_from(prior)
        if pin is None:
            pending[name] = source_build
        else:
            pins[name] = pin
    offline = img.policy.network_mode == "offline"
    if not offline:
        require_host_go(builds.values())
    if pending:
        pins.update(_resolve_for_fetch(img, pending, resolver=resolver, offline=offline))
    root = Path(build) / SOURCES_DIRNAME
    progress = Progress(reporter)
    fetched: list[FetchedSource] = []
    noun = "source" if len(builds) == 1 else "sources"
    copies: dict[tuple[object, str], Path] = {}
    with progress.phase("fetch", f"fetch {len(builds)} {noun}", profile=None):
        for name, source_build in builds.items():
            source = _checkout(
                source_build,
                pins[name],
                root,
                progress,
                offline=offline,
                copies=copies,
            )
            copies.setdefault((source_build.source, source.pin), source.path)
            fetched.append(source)
        prefetches = [
            (index, source_build)
            for index, source_build in enumerate(builds.values())
            if isinstance(source_build, SourceBuild)
        ]
        if not offline:
            pinned = {b.key: checkout_rust_toolchain(b, fetched[i].path) for i, b in prefetches}
            require_host_rustup({key: rust for key, rust in pinned.items() if rust is not None})
        for index, source_build in prefetches:
            source = fetched[index]
            deps = _prefetch(source_build, source, root, progress, offline=offline)
            fetched[index] = replace(source, deps=deps)
    return tuple(fetched)


def _prefetch(
    build: SourceBuild,
    source: FetchedSource,
    root: Path,
    progress: Progress,
    *,
    offline: bool,
) -> str | None:
    """Prefetch *build*'s dependencies next to its checkout (``prefetch_deps``), with a notice."""
    cache = build.build.deps if isinstance(build.build, LanguageBuild) else None
    outcome = prefetch_deps(build, source.pin, source.path, root, offline=offline)
    if outcome is None or cache is None:
        return None
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
            hint="Run `tundravm lock IMAGE` to pin them, then build from that lockfile.",
            context={"operation": "fetch", "sources": names},
        )
    try:
        resolved = resolve_pins(pending, {}, resolver=resolver, offline=offline)
    except LockfileError as exc:
        message = str(exc.args[0]).replace("Cannot lock", "Cannot fetch", 1)
        raise LockfileError(
            message.replace("; nothing written.", "; nothing fetched."),
            hint="Fix the sources above, or run `tundravm lock IMAGE` and build from its lockfile.",
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
) -> FetchedSource:
    """*build*'s checkout of *pin* under *root*: kept when complete, else fetched afresh.

    A complete checkout is verified first (``is_fetched``: a changed one raises
    ``SourceError``). A complete checkout of the same source and pin in *copies* is
    copied instead of fetched again (builds of one repository share it).
    """
    path = root / build.pin_dir(pin)
    ref = build.source.ref if isinstance(build.source, GitSource) else None
    result = FetchedSource(build.key, build.source.kind, build.source.url, pin, path, ref=ref)
    if is_fetched(path, pin, build.source, name=build.key):
        progress.emit("log", None, f"{build.key}: {pin[:12]} already fetched", source="notice")
        return replace(result, cached=True)
    donor = copies.get((build.source, pin))
    if offline and donor is None:
        raise PolicyError(
            f"Cannot fetch {build.name!r}: the build is offline.",
            hint=f"Populate {path} on a connected host (`tundravm build`), or build without "
            "--offline.",
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
            shutil.rmtree(path)  # incomplete: an interrupted fetch
        (scratch / "tree").rename(path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    how = "fetched" if donor is None else f"copied from {donor.name}:"
    progress.emit(
        "log", None, f"{build.key}: {how} {build.source.describe()} at {pin[:12]}", source="notice"
    )
    return result


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
    build: Path,
    reporter: Reporter | None = None,
    lock_source: Path | None = None,
    fetch: bool = True,
    resolver: Resolver | None = None,
    offline: bool = False,
) -> tuple[BakeResult, tuple[Artifact, ...]]:
    """Bake *img* into *build*, frozen against *locked*; the result and its artifacts.

    *locked* (read from *lock_source*, when it came from a file) is written to
    ``build/tundravm.lock`` when that file is absent or identical. A different
    lockfile already there is left alone: the bake reads *lock_source* (or a
    scratch copy of *locked*) instead. ``None`` bakes unfrozen with whatever
    lockfile ``build`` holds.

    *fetch* first checks the source builds out under
    ``build/.sources`` (:func:`fetch_image`, at the pins the bake builds; an
    unfrozen bake builds the pins it resolves). Without *fetch* the checkouts
    must already be there, or the bake fails with ``E_STATE``.

    *offline* bakes as ``Policy(network_mode="offline")`` does: the fetch reuses
    complete checkouts and downloads nothing, every Go, Cargo and .NET build must
    find its prefetched dependencies (else ``E_STATE`` before mkosi runs), and
    mkosi runs the build scripts with ``--with-network=no``.
    """
    if offline and img.policy.network_mode != "offline":
        img = replace(img, policy=replace(img.policy, network_mode="offline"))
    destination = Path(build)
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
        if fetch:
            pinned = locked if locked is not None else _lock_at(lock_path)
            fetched = fetch_image(
                baking,
                None,
                locked=pinned,
                build=destination,
                resolver=resolver,
                reporter=reporter,
            )
            baking = replace(baking, fetched_pins={s.name: s.locked() for s in fetched})
        result = _bake.bake(baking, destination, frozen=locked is not None, reporter=reporter)
        recipe_digest = (
            locked.recipe_digest
            if locked is not None
            else (read_lock(lock_path).recipe_digest if lock_path.is_file() else "")
        )
        warn = _tree_warning(reporter)
        artifacts: list[Artifact] = []
        for name, built in sorted(result.profiles.items()):
            tree_digest = read_variant_tree(destination / "mkosi", [name], warn=warn).digest
            for target, ref in sorted(built.artifacts.items()):
                artifacts.append(
                    Artifact(
                        path=_under(Path(ref.path), destination),
                        variant=name,
                        target=target,
                        sha256=ref.digest or "",
                        recipe_digest=recipe_digest,
                        lock_digest=result.lock_digest or "",
                        tree_digest=tree_digest,
                        lock_sha256=result.lock_sha256,
                        manifest_sha256=ref.manifest_digest,
                        build=destination,
                        boot_payload=None
                        if ref.boot_payload is None
                        else _under(Path(ref.boot_payload), destination),
                        boot_payload_sha256=ref.boot_payload_digest,
                    )
                )
    return result, tuple(artifacts)


def _under(path: Path, base: Path) -> Path:
    """*path* as *base* joined with its path below it, when it is under *base*."""
    try:
        return base / path.resolve().relative_to(base.resolve())
    except ValueError:
        return path


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
    build: Path,
    variants: Sequence[str] | None = None,
    progress: Callable[[str], None] | None = None,
    fetch: bool = True,
) -> tuple[Artifact, ...]:
    """Build *variants* (default: all) with *backend* into *build*, frozen against *lock*.

    Fails before building when the recipe has error diagnostics or drifted
    from *lock*. The source builds are fetched first (:func:`fetch_image`); ``fetch=False``
    builds from the checkouts already in
    ``build/.sources`` and fails with ``E_STATE`` when one is missing.
    *progress* receives the CLI's progress lines.
    """
    names = variant_names(recipe, variants)
    reporter = None if progress is None else TextReporter(_LineStream(progress), live=False)
    try:
        _, artifacts = bake_image(
            lower(recipe, variants=names),
            names,
            locked=lock,
            backend=backend.build_backend(),
            build=build,
            reporter=reporter,
            fetch=fetch,
        )
    finally:
        if reporter is not None:
            reporter.close()
    return artifacts


# ── measure ──────────────────────────────────────────────────────────────


def verify_artifact(artifact: Artifact) -> None:
    """Raise ``ArtifactError`` unless *artifact*'s file still hashes to its recorded sha256."""
    context = {"variant": artifact.variant, "target": artifact.target, "path": str(artifact.path)}
    rebake = "Build the profile again (`tundravm build`) to record the artifact it should be."
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
            f"Artifact {artifact.path} changed since it was built.",
            hint=f"The build recorded sha256 {artifact.sha256[:12]}; {rebake}",
            context={**context, "recorded": artifact.sha256, "actual": actual},
        )


def measure(artifact: Artifact, *, scheme: Scheme = "qemu") -> Measurements:
    """Expected TDX measurements of *artifact* on the hosts of *scheme*.

    tundravm replays the boot event log from the UKI (and the disk's GPT) itself;
    ``Measurements.unverifiable`` names the registers the scheme cannot predict.

    The artifact's bytes are checked against their recorded sha256 first
    (``verify_artifact``). An artifact that is no UKI or disk raises
    ``MeasurementError``. A disk artifact whose bake recorded the UKI it boots is
    measured through that UKI when no tool reads the disk's format (the UKI's bytes
    are checked against their recorded sha256 too); the result names the UKI as
    its boot payload, so the policy covers every disk that boots it.
    """
    verify_artifact(artifact)
    uki = _measured_uki(artifact)
    target = artifact.target
    profile = ProfileBuildResult(
        profile=artifact.variant,
        artifacts={
            target: ArtifactRef(
                target=target,
                path=artifact.path,
                digest=artifact.sha256,
                boot_payload=None if uki is None or uki[0] == artifact.path else uki[0],
            )
        },
    )
    found = derive_measurements(
        backend=scheme,
        profile=artifact.variant,
        profile_result=profile,
    )
    tool = str(found.source)
    if found.tool_version is not None:
        tool += f" {found.tool_version}"
    registers = {name: tuple(entries) for name, entries in found.registers.items()}
    return Measurements(
        scheme=scheme,
        values=tuple(sorted(single_values(found.registers).items())),
        registers=tuple(sorted(registers.items())),
        unverifiable=tuple(sorted(found.unverifiable.items())),
        extra=tuple(sorted(found.extra.items())),
        tool=tool,
        artifact_digest=artifact.sha256,
        artifact_path=str(artifact.path),
        boot_payload_path="" if uki is None else str(uki[0]),
        boot_payload_digest="" if uki is None else uki[1],
    )


def _measured_uki(artifact: Artifact) -> tuple[Path, str] | None:
    """The UKI a measurement of *artifact* is of, with its sha256: the artifact itself when it
    is one (``.efi``), else the boot payload the bake recorded, checked against its sha256."""
    if artifact.path.suffix == ".efi":
        return artifact.path, artifact.sha256
    if artifact.boot_payload is None or not artifact.boot_payload_sha256:
        return None
    payload = Path(artifact.boot_payload)
    verify_artifact(
        replace(artifact, path=payload, sha256=artifact.boot_payload_sha256, boot_payload=None)
    )
    return payload, artifact.boot_payload_sha256


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
                    "warning" if requirement.optional else "error",
                    f"{requirement.tool} is missing: {requirement.hint}",
                    subject=requirement.tool,
                )
            )
    return tuple(found)


__all__ = [
    "Artifact",
    "Backend",
    "BackendKind",
    "Entry",
    "FetchedSource",
    "Lock",
    "Measurements",
    "Pin",
    "Scheme",
    "Tree",
    "bake",
    "compile",
    "doctor",
    "lint",
    "lock",
    "lock_status",
    "measure",
    "read_lock",
    "read_tree",
    "verify_artifact",
    "write_lock",
]
