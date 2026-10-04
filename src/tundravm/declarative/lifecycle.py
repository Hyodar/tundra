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
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Literal, get_args

from tundravm import check as _check
from tundravm._image import Image
from tundravm._source import Resolver, resolve_pins
from tundravm.backends import (
    InProcessBackend,
    LimaMkosiBackend,
    LocalLinuxBackend,
    NixMkosiBackend,
    Requirement,
)
from tundravm.backends.base import BuildBackend
from tundravm.deploy import DeployAdapter, get_adapter
from tundravm.diff import diff_trees
from tundravm.errors import DeploymentError, MeasurementError, StateError, ValidationError
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
from tundravm.observability import Reporter, TextReporter

from .lower import lower
from .model import Diagnostic, Git, Http, Pairs, Recipe, Target
from .resolve import lint as _resolution_lint

Scheme = Literal["rtmr", "azure", "gcp"]
BackendKind = Literal["lima", "nix", "local", "inprocess"]
ProbeRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

LOCK_FILENAME = "tundravm.lock"
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
    """A lockfile as a value; ``sections`` maps payload sections to their digests."""

    recipe_digest: str
    sections: Pairs
    pins: tuple[Pin, ...]
    compiler_version: str
    lockfile: Lockfile = field(compare=False, repr=False)

    @classmethod
    def of(cls, lockfile: Lockfile) -> Lock:
        return cls(
            recipe_digest=lockfile.recipe_digest,
            sections=tuple(sorted(lockfile.sections.items())),
            pins=tuple(_pin(fetch) for fetch in lockfile.fetches),
            compiler_version=_version(),
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
    """A compiled mkosi project held in memory; ``variants`` are its top-level directories."""

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


@dataclass(frozen=True, slots=True)
class Azure:
    storage_account: str
    resource_group: str = "tdx-vms"
    location: str = "eastus"
    vm_size: str = "Standard_DC2s_v3"


@dataclass(frozen=True, slots=True)
class Gcp:
    project: str
    bucket: str
    zone: str = "us-central1-a"
    machine_type: str = "n2d-standard-2"


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
def using_lock(img: Image, locked: Lock | None) -> Iterator[Path]:
    """Point *img* at a scratch build dir holding *locked* (or no lockfile) while it runs."""
    saved = img.build_dir
    with tempfile.TemporaryDirectory(prefix="tundravm-lock-") as tmp:
        img.build_dir = Path(tmp)
        if locked is not None:
            (img.build_dir / LOCK_FILENAME).write_text(locked.text(), encoding="utf-8")
        try:
            yield img.build_dir
        finally:
            img.build_dir = saved


def compile_image(img: Image, profiles: Sequence[str] | None, *, locked: Lock | None) -> Tree:
    """The tree *img* compiles to for *profiles*, without touching its compile cache."""
    saved = (img._last_compile_digest, img._last_compile_path, img._last_compile_emission)
    with tempfile.TemporaryDirectory(prefix="tundravm-tree-") as tmp:
        try:
            with using_lock(img, locked):  # never the working directory's build/ lockfile
                result = img.compile(Path(tmp), force=True, profiles=profiles)
        finally:
            img._last_compile_digest, img._last_compile_path, img._last_compile_emission = saved
        return read_tree(Path(tmp), variants=result.profiles)


def read_tree(root: Path, *, variants: Sequence[str] = ()) -> Tree:
    """Load the tree at *root* into memory."""
    entries: list[Entry] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        base = Path(dirpath)
        for name in sorted([*dirnames, *filenames]):
            path = base / name
            rel = path.relative_to(root).as_posix()
            info = path.lstat()
            mode = info.st_mode & 0o7777
            if path.is_symlink():
                entries.append(Entry(rel, None, mode, symlink=os.readlink(path)))
            elif path.is_dir():
                if not any(path.iterdir()):
                    entries.append(Entry(rel, None, mode))
            else:
                entries.append(Entry(rel, path.read_bytes(), mode))
    entries.sort(key=lambda e: e.path)
    return Tree(entries=tuple(entries), digest=_tree_digest(entries), variants=tuple(variants))


def _tree_digest(entries: Sequence[Entry]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry.path.encode() + b"\0" + f"{entry.mode:o}".encode() + b"\0")
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


def check_report(
    recipe: Recipe | None, img: Image | None, *, variants: Sequence[str] | None
) -> list[_check.Diagnostic]:
    """Declarative and compiler diagnostics in the compiler's report form.

    The recipe's resolution diagnostics come first, in resolution order. When
    none is an error, the lowered image's ``check()`` findings follow, sorted.
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

    With *lock*, drift against it is reported too (:func:`lock_status`).
    """
    found = [
        Diagnostic(
            code=d.code,
            message=d.message,
            level=d.level,
            variant="" if d.profile == "common" else d.profile,
            subject=d.subject or "",
        )
        for d in check_report(recipe, None, variants=variants)
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
    img: Image,
    profiles: Sequence[str] | None,
    *,
    previous: Lock | None = None,
    update: Sequence[str] = (),
    offline: bool = False,
    resolver: Resolver | None = None,
) -> Lock:
    """Lock *img*'s *profiles*, keeping *previous* pins except for the *update* sources."""
    offline = offline or img.policy.network_mode == "offline"
    with img._operation_scope(profiles) as names:
        payload = img._recipe_payload(profile_names=names)
        builds = img.source_builds()
    unknown = [name for name in update if name not in builds]
    if unknown:
        raise ValidationError(
            f"Cannot update unknown source(s): {', '.join(unknown)}.",
            hint=f"Declared sources: {', '.join(builds) or '(none)'}",
        )
    prior = {} if previous is None else _named_pins(previous)
    kept: list[LockedFetch] = []
    pending = {}
    for name, build in builds.items():
        pin = None if name in update else build.pin_from(prior)
        if pin is None:
            pending[name] = build
        else:
            kept.append(build.locked(pin))
    fetches = [*kept, *resolve_pins(pending, prior, resolver=resolver, offline=offline)]
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
    *update* are resolved again. *resolver* replaces the network lookup;
    ``offline=True`` fails for any source *previous* does not pin.
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
    img: Image,
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
    with img._operation_scope(profiles):
        drift = img._lock_drift(locked.lockfile, resolver=resolver, partial=partial)
    details = drift.details

    def lines() -> Iterator[tuple[str, str, str]]:
        for section in drift.changed:
            detail = f": {details[section]}" if section in details else ""
            yield "lock-changed", section, f"{section} changed since the lock{detail}"
        for section in drift.added:
            yield "lock-added", section, f"{section} is not in the lock"
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
    img: Image,
    profiles: Sequence[str] | None,
    *,
    locked: Lock | None,
    backend: BuildBackend | None,
    out: Path,
    reporter: Reporter | None = None,
    lock_source: Path | None = None,
) -> tuple[BakeResult, tuple[Artifact, ...]]:
    """Bake *img* into *out*, frozen against *locked*, and record the artifact manifest.

    *locked* (read from *lock_source*, when it came from a file) is written to
    ``out/tundravm.lock`` when that file is absent or identical. A different
    lockfile already there is left alone: the bake reads *lock_source* (or a
    scratch copy of *locked*) instead, and ``bake-result.json`` records which
    lockfile it used. ``None`` bakes unfrozen with whatever lockfile ``out`` holds.
    """
    destination = Path(out)
    destination.mkdir(parents=True, exist_ok=True)
    lock_path = destination / LOCK_FILENAME
    with ExitStack() as stack:
        used = _bake_lock(locked, lock_path, lock_source, stack)
        saved = (img.build_dir, img.backend, img.lock_file)
        img.build_dir = destination
        img.lock_file = None if used == lock_path else used
        if backend is not None:
            img.backend = backend
        try:
            result = img.bake(
                destination, frozen=locked is not None, reporter=reporter, profiles=profiles
            )
            simulated = img.backend is not None and img.backend.name == INPROCESS
        finally:
            img.build_dir, img.backend, img.lock_file = saved
    tree = read_tree(destination / "mkosi")
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
    if locked is not None:
        source = lock_source if lock_source is not None else lock_path
        # None: an in-memory Lock read through a scratch copy
        payload[MANIFEST_KEY]["lockfile"] = (
            str(source) if lock_source is not None or used == lock_path else None
        )
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result, read_artifacts(manifest)


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
    locked: Lock,
    backend: Backend,
    out: Path,
    variants: Sequence[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[Artifact, ...]:
    """Build *variants* (default: all) with *backend* into *out*, frozen against *locked*.

    Fails before building when the recipe has error diagnostics or drifted
    from *locked*. *progress* receives the CLI's progress lines. Writes
    ``out/bake-result.json``, which :func:`read_artifacts` reads back.
    In-process artifacts are ``simulated``.
    """
    names = variant_names(recipe, variants)
    reporter = None if progress is None else TextReporter(_LineStream(progress), live=False)
    try:
        _, artifacts = bake_image(
            lower(recipe, variants=names),
            names,
            locked=locked,
            backend=backend.build_backend(),
            out=out,
            reporter=reporter,
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
        raise StateError(f"Unreadable bake result: {exc}", context={"path": str(path)}) from exc
    simulated = bool(extra.get("simulated", result.backend == INPROCESS))
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


def measure(
    artifact: Artifact, *, scheme: Scheme = "rtmr", allow_placeholder: bool = False
) -> Measurements:
    """Expected measurements of *artifact* from ``measured-boot``/``dstack-mr``.

    Without a tool this raises ``MeasurementError``; *allow_placeholder* returns
    digest-derived values instead (``tool="placeholder"``). Simulated artifacts
    are refused unless *allow_placeholder*.
    """
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
    """*using*'s fields as the adapter's string parameters."""
    params: dict[str, str] = {}
    for item in fields(using):
        value = getattr(using, item.name)
        params[item.name] = str(value).lower() if isinstance(value, bool) else str(value)
    return params


def target_of(using: DeployTarget) -> Target:
    return next(name for name, kind in DEPLOY_TARGETS.items() if isinstance(using, kind))


def deploy(
    artifact: Artifact,
    *,
    using: DeployTarget,
    allow_placeholder: bool = False,
    adapter: DeployAdapter | None = None,
) -> Deployment:
    """Deploy *artifact* with *using*'s settings; its target must match the artifact's.

    Simulated artifacts are refused unless *allow_placeholder*. *adapter*
    replaces the target's default adapter (tests).
    """
    target = target_of(using)
    if artifact.simulated and not allow_placeholder:
        raise DeploymentError(
            "Refusing to deploy a simulated artifact.",
            hint="Bake with a real backend first.",
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
        parameters=deploy_parameters(using),
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
    "lint",
    "lock",
    "lock_status",
    "measure",
    "read_artifacts",
    "read_lock",
    "read_tree",
    "write_lock",
]
