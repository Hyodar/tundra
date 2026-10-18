"""``tundravm status``: where a recipe project stands, read-only and network-free.

One :class:`StatusItem` per thing the lifecycle produces (the lockfile, each
source checkout, the mkosi tree, each baked artifact and its variant's package
manifest) plus the recipe, its lint summary and the build backend's host tools,
each with a verdict. Past the bake it is a release-readiness view: one ``release``
line per variant eligible for release, ``ready`` or ``blocked`` with the reason codes
:func:`~tundravm.evidence.evidence` gives against the required checks, and one per
release of the recipe still in progress. ``next`` is the single most useful command
to run next: lock, fetch, compile, bake, then a second build, ``measure``, ``attest``
and ``release``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from ._hashes import HashCache
from ._source import SOURCES_DIRNAME, is_fetched
from .check import summarize
from .declarative._lowered import Lowered
from .declarative.bom import manifest_path
from .declarative.lifecycle import (
    INPROCESS,
    NO_LOCK,
    Artifact,
    Lock,
    ProbeRunner,
    check_report,
    compile_image,
    probe,
    read_artifacts,
    read_lock,
    recorded_reproducibility,
    reproducibility_of,
    requirements_of,
    using_lock,
    variant_provenance,
    variant_tree_digest,
    verify_artifact,
)
from .diff import diff_against
from .errors import SourceError, TdxError, ValidationError
from .evidence import evidence
from .formats import md_cell, md_table
from .lockfile import lock_variants
from .models import BAKE_RESULT_FILENAME
from .recipe import RecipeFile
from .release import INDEX_NAME as RELEASE_INDEX
from .release import previous_release, releases_in_progress

Verdict = Literal["ok", "stale", "missing", "n/a", "error", "blocked", "pending"]
Integrity = Literal["verified", "unchecked", "mismatch"]
"""An artifact's bytes against their recorded sha256; ``unchecked`` unless ``verify``."""
VERDICTS: tuple[Verdict, ...] = ("ok", "stale", "missing", "n/a", "error", "blocked", "pending")
"""``error`` is only the lint verdict: the recipe has error diagnostics. ``blocked`` is only
a ``release`` line's (a required check is missing, or something contradicts the bake), and
``pending`` a release in progress."""

UP_TO_DATE = "everything is up to date"
"""``next`` when the build is current, every eligible variant is ready and the newest
finished release was cut from this recipe and lockfile: never while a release is blocked."""
BLOCKED = "release blocked"
"""How ``next`` starts when readiness is blocked by something no single command fixes."""
DEFAULT_REQUIRE: tuple[str, ...] = ("reproducible", "lock")
"""The checks release readiness is judged against without ``[tool.tundravm] require``."""
ATTESTATION_NAME = "attestation.json"
"""``BUILD/<variant>/attestation.json``: the ``attest --record`` file readiness bundles."""
TREE_DIRNAME = "mkosi"

_RANK: dict[Verdict, int] = {
    "n/a": 0,
    "ok": 1,
    "pending": 2,
    "stale": 2,
    "blocked": 3,
    "missing": 3,
    "error": 4,
}
_REBAKE = frozenset(
    {"recipe-mismatch", "tree-mismatch", "integrity-mismatch", "artifact-missing", "lock-unbound"}
)
_REPRODUCE = frozenset({"reproducibility-unchecked", "reproducibility-incomplete"})
_MEASURE = frozenset(
    {
        "policy-missing",
        "policy-unbound",
        "policy-invalid",
        "policy-placeholder",
        "policy-artifact-mismatch",
        "artifact-unmeasured",
    }
)
_ATTEST = frozenset(
    {
        "attestation-missing",
        "attestation-policy-mismatch",
        "attestation-untrusted",
        "attestation-partial",
        "attestation-stale",
    }
)
_NEEDS_REAL = _MEASURE | _ATTEST | {"simulated-artifact"}
"""Codes a simulated (in-process) artifact can never clear: it needs a real bake."""
_LABEL_WIDTH = 10
_VERDICT_WIDTH = 9


@dataclass(frozen=True, slots=True)
class StatusItem:
    """One report line: *label* (``lock``, ``source``, ...), its verdict and a one-line detail."""

    label: str
    verdict: Verdict
    detail: str
    data: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {"verdict": self.verdict, "detail": self.detail, **self.data}


@dataclass(frozen=True, slots=True)
class Invocation:
    """How ``status`` was called, so ``next`` repeats the paths and variants it was given."""

    recipe: str
    """RECIPE as ``next`` repeats it: empty when it came from ``[tool.tundravm]``."""
    build: Path | None = None
    """``--build`` as ``next`` repeats it: ``None`` unless given on the command line."""
    lockfile: Path | None = None
    lock_configured: bool = False
    """Whether the lockfile is ``[tool.tundravm]``'s, reported as configured when missing."""
    variants: tuple[str, ...] = ()
    tree_flag: bool = True
    """Whether a ``compile`` suggestion spells out ``--tree TREE`` (not when configured)."""
    recipe_path: str = ""
    """The path the ``recipe`` line shows; empty: ``recipe``."""
    releases: Path | None = None
    """``--releases`` as ``next`` repeats it: ``None`` unless given on the command line."""
    backend: str | None = None
    """The backend kind a release would bake with, to tell a resume that needs ``--backend``."""


@dataclass(frozen=True, slots=True)
class ProjectStatus:
    recipe: StatusItem
    lint: StatusItem
    lock: StatusItem
    sources: tuple[StatusItem, ...]
    tree: StatusItem
    manifest: Path
    artifacts: tuple[StatusItem, ...]
    manifests: tuple[StatusItem, ...]
    """One ``manifest`` line per baked variant: mkosi's package manifest beside its artifact."""
    backend: StatusItem
    next: str
    releases: tuple[StatusItem, ...] = ()
    """One ``release`` line per selected variant eligible for release: ``ok`` (ready) or
    ``blocked``; one ``n/a`` line when none is eligible."""
    in_progress: tuple[StatusItem, ...] = ()
    """One ``release`` line (``pending``) per unfinished release of the recipe."""
    require: tuple[str, ...] = DEFAULT_REQUIRE
    """The checks readiness was judged against."""

    def items(self) -> tuple[StatusItem, ...]:
        """Every line in report order."""
        sources = self.sources or (StatusItem("source", "n/a", "no source builds or kernels"),)
        return (
            self.recipe,
            self.lint,
            self.lock,
            *sources,
            self.tree,
            *self.artifacts,
            *self.manifests,
            self.backend,
            *self.releases,
            *self.in_progress,
        )

    def to_dict(self) -> dict[str, object]:
        """The JSON form: one key per section, each holding a ``verdict``."""
        return {
            "recipe": self.recipe.to_dict(),
            "lint": self.lint.to_dict(),
            "lock": self.lock.to_dict(),
            "sources": {
                "verdict": overall(self.sources),
                "items": [item.to_dict() for item in self.sources],
            },
            "tree": self.tree.to_dict(),
            "artifacts": {
                "verdict": overall(self.artifacts),
                "manifest": str(self.manifest),
                "present": self.manifest.is_file(),
                "items": [item.to_dict() for item in self.artifacts],
            },
            "manifests": [dict(item.data) for item in self.manifests],
            "backend": self.backend.to_dict(),
            "release": {
                str(item.data["variant"]): {
                    "ready": item.data["ready"],
                    "reasons": item.data["reasons"],
                    "released": item.data["released"],
                    "verdict": item.verdict,
                    "detail": item.detail,
                }
                for item in self.releases
                if "variant" in item.data
            },
            "release_require": list(self.require),
            "releases_in_progress": [dict(item.data) for item in self.in_progress],
            "next": self.next,
        }


def overall(items: Sequence[StatusItem]) -> Verdict:
    """The worst verdict of *items* (``n/a`` when there are none)."""
    return max((item.verdict for item in items), key=_RANK.__getitem__, default="n/a")


def project_status(
    loaded: RecipeFile,
    variants: Sequence[str],
    *,
    build: Path,
    lock_path: Path,
    runner: ProbeRunner,
    invocation: Invocation,
    verify: bool = False,
    tree: Path | None = None,
    require: Sequence[str] = DEFAULT_REQUIRE,
    releases: Path = Path("releases"),
) -> ProjectStatus:
    """Report *variants* of *loaded* against the build directory *build*; never writes.

    *tree* is the compiled mkosi tree (default: ``BUILD/mkosi``). With *verify*
    each artifact's file is hashed against its recorded sha256.

    Each selected variant eligible for release (``Variant(release=True)``) gets a
    readiness line: the evidence its bake would give (:func:`~tundravm.evidence.evidence`,
    hashing its artifacts, with ``BUILD/<variant>/policy.json`` and, when present,
    ``BUILD/<variant>/attestation.json``) against *require*, ready only when its verdict
    would pass. *releases* is searched for unfinished releases of the recipe and for
    the newest finished one.
    """
    tree_path = tree if tree is not None else build / TREE_DIRNAME
    img = loaded.lowered()
    names = tuple(variants)
    lock, lock_item = _lock(img, names, lock_path, configured=invocation.lock_configured)
    sources = _sources(img, names, lock, build)
    tree_item = _tree(img, names, lock, tree_path)
    manifest = build / BAKE_RESULT_FILENAME
    required = tuple(require)
    released = _released(img, lock_path, releases, loaded.recipe.name)
    with nullcontext() if verify else HashCache(build).using():
        artifacts, baked = _artifacts(img, names, lock, manifest, verify=verify)
        readiness = _readiness(
            loaded,
            names,
            baked,
            build=build,
            lock_path=lock_path,
            runner=runner,
            require=required,
            released=released,
        )
    backend = _backend(loaded, runner)
    lint = _lint(loaded, names, lock)
    status = ProjectStatus(
        recipe=_recipe(loaded, img, names, invocation.recipe_path or invocation.recipe),
        lint=lint,
        lock=lock_item,
        sources=sources,
        tree=tree_item,
        manifest=manifest,
        artifacts=artifacts,
        manifests=_manifests(baked, names),
        backend=backend,
        next="",
        releases=readiness,
        in_progress=_in_progress(loaded, releases, build, invocation),
        require=required,
    )
    return replace(status, next=_next(status, invocation, tree_path))


def _recipe(loaded: RecipeFile, img: Lowered, names: tuple[str, ...], shown: str) -> StatusItem:
    digest = img.select(names).digest()
    recipe = loaded.recipe
    data = {
        "path": shown,
        "digest": digest,
        "variants": list(names),
        "dialect": img.mkosi.dialect,
        "base": recipe.platform.name,
        "snapshot": recipe.platform.snapshot,
    }
    detail = (
        f"{shown}  digest {digest[:12]}  variants {', '.join(names)}  "
        f"dialect {img.mkosi.dialect}  base {recipe.platform.name}"
        + (f"  snapshot {recipe.platform.snapshot}" if recipe.platform.snapshot else "")
    )
    return StatusItem("recipe", "ok", detail, data)


def _lint(loaded: RecipeFile, names: tuple[str, ...], lock: Lock | None) -> StatusItem:
    """Lint against *lock*, the one status selected; ``None`` is no lockfile, never the default."""
    selected = NO_LOCK if lock is None else lock
    counts = summarize(check_report(loaded.recipe, loaded.image, variants=names, lock=selected))
    detail = ", ".join(f"{count} {level}" for level, count in counts.items())
    verdict: Verdict = "error" if counts["errors"] else "ok"
    return StatusItem("lint", verdict, detail, dict(counts))


def _lock(
    img: Lowered, names: tuple[str, ...], path: Path, *, configured: bool = False
) -> tuple[Lock | None, StatusItem]:
    scoped = img.select(names)
    builds = scoped.lock_sources()
    data: dict[str, object] = {"path": str(path), "present": path.is_file()}
    if not path.is_file():
        data.update(up_to_date=False, drift=[], unpinned=sorted(builds), version=None)
        detail = (
            f"configured lockfile {path} does not exist; run tundravm lock"
            if configured
            else f"{path}: no lockfile"
        )
        return None, StatusItem("lock", "missing", detail, data)
    try:
        lock = read_lock(path)
        drift = scoped.lock_status(path)
    except TdxError as exc:
        data.update(up_to_date=False, drift=[], unpinned=sorted(builds), version=None)
        return None, StatusItem("lock", "stale", f"{path}: unreadable: {exc}", data)
    pins = {f.name: f for f in lock.lockfile.fetches if f.name is not None}
    unpinned = sorted(name for name, build in builds.items() if build.pin_from(pins) is None)
    data.update(
        up_to_date=drift.is_clean,
        drift=list(drift.sections),
        unpinned=unpinned,
        version=lock.lockfile.version,
    )
    parts = [f"{path}", f"v{lock.lockfile.version}"]
    if drift.is_clean:
        parts.append("up to date")
    else:
        count = len(drift.sections) or 1
        parts.append(f"{count} section{'' if count == 1 else 's'} drifted")
    if unpinned:
        parts.append(f"unpinned: {', '.join(unpinned)}")
    verdict: Verdict = "ok" if drift.is_clean and not unpinned else "stale"
    return lock, StatusItem("lock", verdict, "  ".join(parts), data)


def _sources(
    img: Lowered, names: tuple[str, ...], lock: Lock | None, build: Path
) -> tuple[StatusItem, ...]:
    builds = img.select(names).lock_sources()
    pins = {} if lock is None else {f.name: f for f in lock.lockfile.fetches if f.name is not None}
    root = build / SOURCES_DIRNAME
    simulated = img.backend is not None and img.backend.name == INPROCESS
    items: list[StatusItem] = []
    for name, spec in builds.items():
        pin = spec.pin_from(pins)
        data: dict[str, object] = {"name": name, "pin": pin, "path": None, "fetched": False}
        if not img.fetches_sources:
            detail = f"{name}: fetched inside the build ({img.mkosi.dialect})"
            items.append(StatusItem("source", "n/a", detail, data))
            continue
        if pin is None:
            detail = f"{name}: not pinned, so no checkout to check"
            items.append(StatusItem("source", "n/a" if simulated else "missing", detail, data))
            continue
        path = root / spec.pin_dir(pin)
        data["path"] = str(path)
        others = _other_checkouts(root, name, path)
        try:
            fetched = is_fetched(path, pin, spec.source, name=name)
        except SourceError as exc:
            data["fetched"] = False
            data["modified"] = exc.reason
            items.append(
                StatusItem(
                    "source",
                    "stale",
                    f"{name}: checkout at {path} modified since the fetch ({exc.reason}); "
                    "run tundravm fetch --force",
                    data,
                )
            )
            continue
        if fetched:
            data["fetched"] = True
            items.append(StatusItem("source", "ok", f"{name}: {pin[:12]} at {path}", data))
            continue
        if others:
            detail = f"{name}: {others[0]} holds another pin; locked {pin[:12]}"
            verdict: Verdict = "stale"
        elif path.exists():
            detail, verdict = f"{name}: {path} is incomplete", "stale"
        else:
            detail, verdict = f"{name}: {pin[:12]} not fetched into {root}", "missing"
        if simulated:
            detail, verdict = f"{detail} (the inprocess backend does not need it)", "n/a"
        items.append(StatusItem("source", verdict, detail, data))
    return tuple(items)


def _other_checkouts(root: Path, name: str, path: Path) -> list[str]:
    pattern = re.compile(rf"{re.escape(name)}-[0-9a-f]{{12}}")
    if not root.is_dir():
        return []
    return sorted(
        child.name
        for child in root.iterdir()
        if child != path and child.is_dir() and pattern.fullmatch(child.name)
    )


def _tree(img: Lowered, names: tuple[str, ...], lock: Lock | None, path: Path) -> StatusItem:
    data: dict[str, object] = {"path": str(path), "present": path.is_dir(), "changed": []}
    if not path.is_dir():
        return StatusItem("tree", "missing", f"{path}: not compiled", data)
    try:
        with using_lock(img, lock) as pinned:
            result = diff_against(pinned, path, profiles=names)
    except TdxError as exc:
        return StatusItem("tree", "n/a", f"{path}: cannot compile: {exc}", data)
    changed = sorted(change.path for change in result.changes)
    data["changed"] = changed
    if result.is_clean:
        return StatusItem("tree", "ok", f"{path}: up to date with the recipe", data)
    count = len(changed)
    detail = f"{path}: {count} file{'s differ' if count != 1 else ' differs'} from the recipe"
    return StatusItem("tree", "stale", detail, data)


def _artifacts(
    img: Lowered, names: tuple[str, ...], lock: Lock | None, manifest: Path, *, verify: bool
) -> tuple[tuple[StatusItem, ...], tuple[Artifact, ...]]:
    """The ``artifact`` lines of *names*, and the artifacts *manifest* records."""
    if not manifest.is_file():
        return tuple(
            StatusItem(
                "artifact", "missing", f"{name}: not baked (no {manifest})", {"variant": name}
            )
            for name in names
        ), ()
    try:
        artifacts = read_artifacts(manifest.parent)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (TdxError, OSError, ValueError) as exc:
        detail = f"{manifest} is unreadable: {exc}"
        return tuple(StatusItem("artifact", "stale", detail, {"variant": n}) for n in names), ()
    current = _current_digests(img, lock, artifacts)
    items: list[StatusItem] = []
    for name in names:
        baked = [artifact for artifact in artifacts if artifact.variant == name]
        if not baked:
            detail = f"{name}: not in {manifest}"
            items.append(StatusItem("artifact", "missing", detail, {"variant": name}))
        own = variant_provenance(payload, name)
        lockfile = own.get("lockfile")
        recorded = recorded_reproducibility(own)
        for artifact in baked:
            subject = f"{artifact.variant}/{artifact.target}"
            reproducibility = reproducibility_of(recorded, subject)
            items.append(
                _artifact(
                    artifact, current, lockfile, verify=verify, reproducibility=reproducibility
                )
            )
    return tuple(items), tuple(artifacts)


def _manifests(artifacts: Sequence[Artifact], names: tuple[str, ...]) -> tuple[StatusItem, ...]:
    """Per baked variant of *names*, whether mkosi's package manifest is beside its artifact."""
    items: list[StatusItem] = []
    for name in names:
        artifact = next((a for a in artifacts if a.variant == name), None)
        if artifact is None:
            continue
        path = manifest_path(artifact)
        present = path.is_file()
        data: dict[str, object] = {"variant": name, "path": str(path), "present": present}
        verdict: Verdict
        if artifact.simulated:
            verdict, detail = "n/a", f"{path}  simulated: nothing was installed"
        elif present:
            verdict, detail = "ok", str(path)
        else:
            verdict, detail = "missing", f"{path}  sbom lists declared packages only"
        items.append(StatusItem("manifest", verdict, detail, data))
    return tuple(items)


def _current_digests(
    img: Lowered, lock: Lock | None, artifacts: Sequence[Artifact]
) -> tuple[dict[str, str], set[str]]:
    """The tree digest each baked variant compiles to now, and the recipe digests to accept."""
    declared = set(img.state.profiles)
    baked = tuple(sorted({artifact.variant for artifact in artifacts} & declared))
    if not baked:
        return {}, set()
    try:
        compiled = compile_image(img, baked, locked=lock)
        tree = {name: variant_tree_digest(compiled, name) for name in baked}
    except TdxError:
        tree = {}
    scopes = {tuple(sorted(declared)), baked}
    if lock is not None and set(lock_variants(lock.lockfile)) <= declared:
        scopes.add(lock_variants(lock.lockfile))
    recipes = {img.select(scope).digest() for scope in scopes}
    return tree, recipes


def _integrity(artifact: Artifact, *, verify: bool) -> Integrity:
    if not verify:
        return "unchecked"
    try:
        verify_artifact(artifact)
    except TdxError:
        return "mismatch"
    return "verified"


def _artifact(
    artifact: Artifact,
    current: tuple[dict[str, str], set[str]],
    lockfile: object,
    *,
    verify: bool,
    reproducibility: str | None = None,
) -> StatusItem:
    """*reproducibility* is the state ``bake --verify-reproducible`` recorded for the artifact
    (``matched``, ``different``, ``running``, ``error``, ``interrupted``); ``None``: not checked."""
    tree, recipes = current
    path = artifact.path
    size = path.stat().st_size if path.is_file() else None
    integrity = _integrity(artifact, verify=verify and size is not None)
    recipe_ok = None if not artifact.recipe_digest else artifact.recipe_digest in recipes
    baked_tree = artifact.tree_digest
    tree_ok = None if not baked_tree else baked_tree == tree.get(artifact.variant)
    data: dict[str, object] = {
        "variant": artifact.variant,
        "target": artifact.target,
        "path": str(path),
        "size": size,
        "sha256": artifact.sha256,
        "simulated": artifact.simulated,
        "lockfile": lockfile if isinstance(lockfile, str) else None,
        "recipe_matches": recipe_ok,
        "tree_matches": tree_ok,
        "integrity": integrity,
        "reproducibility": reproducibility,
    }
    parts = [f"{artifact.variant}/{artifact.target}", str(path)]
    if size is None:
        parts.append("file is gone")
    else:
        parts.extend((_size(size), f"sha256 {artifact.sha256[:12]}"))
        parts.append(
            "integrity unchecked (--verify hashes it)"
            if integrity == "unchecked"
            else f"integrity {integrity}"
        )
    if reproducibility is not None:
        parts.append(f"reproducibility {reproducibility}")
    if artifact.simulated:
        parts.append("simulated")
    parts.append(f"lock {lockfile}" if isinstance(lockfile, str) else "unpinned")
    changed = [what for what, ok in (("recipe", recipe_ok), ("tree", tree_ok)) if ok is False]
    if changed:
        parts.append(f"{' and '.join(changed)} changed since the bake")
    if integrity == "mismatch":
        changed.append("bytes")
    verdict: Verdict = "missing" if size is None else ("stale" if changed else "ok")
    return StatusItem("artifact", verdict, "  ".join(parts), data)


def _size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{size} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _released(img: Lowered, lock_path: Path, releases: Path, recipe: str) -> dict[str, str]:
    """Per variant, the newest finished release of *recipe* when it was cut from this recipe
    (every variant's digest) and this lockfile's bytes; empty otherwise."""
    found = previous_release(releases, recipe)
    if found is None or not lock_path.is_file():
        return {}
    try:
        payload = json.loads((found / RELEASE_INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    digest = img.select(tuple(img.state.profiles)).digest()
    lock_sha256 = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    if payload.get("recipe_digest") != digest or payload.get("lock_sha256") != lock_sha256:
        return {}
    return {str(name): str(found) for name in payload.get("variants") or ()}


def _readiness(
    loaded: RecipeFile,
    names: tuple[str, ...],
    baked: Sequence[Artifact],
    *,
    build: Path,
    lock_path: Path,
    runner: ProbeRunner,
    require: tuple[str, ...],
    released: Mapping[str, str],
) -> tuple[StatusItem, ...]:
    """One ``release`` line per variant of *names* eligible for release."""
    declared = {variant.name: variant for variant in loaded.recipe.variants}
    eligible = [name for name in names if name in declared and declared[name].release]
    shown = ", ".join(require) or "nothing"
    if not eligible:
        detail = "no selected variant is eligible for release (Variant(release=False))"
        return (StatusItem("release", "n/a", detail, {"require": list(require)}),)
    items: list[StatusItem] = []
    for name in eligible:
        reasons = _blockers(
            loaded,
            name,
            baked=any(artifact.variant == name for artifact in baked),
            build=build,
            lock_path=lock_path,
            runner=runner,
            require=require,
        )
        data: dict[str, object] = {
            "variant": name,
            "ready": not reasons,
            "reasons": reasons,
            "require": list(require),
            "released": None if reasons else released.get(name),
        }
        if reasons:
            codes = ", ".join(dict.fromkeys(str(reason["code"]) for reason in reasons))
            detail = f"{name}: blocked ({codes})  requires {shown}"
            items.append(StatusItem("release", "blocked", detail, data))
            continue
        tail = f"  released as {released[name]}" if name in released else ""
        items.append(StatusItem("release", "ok", f"{name}: ready  requires {shown}{tail}", data))
    return tuple(items)


def _blockers(
    loaded: RecipeFile,
    name: str,
    *,
    baked: bool,
    build: Path,
    lock_path: Path,
    runner: ProbeRunner,
    require: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Why *name*'s evidence would not pass against *require* (evidence's reasons)."""
    if not baked:
        return [_reason("not-baked", name, f"{name} is not baked")]
    record = build / name / ATTESTATION_NAME

    def gather(records: Sequence[Path]) -> list[dict[str, Any]]:
        found = evidence(
            loaded.recipe,
            build=build,
            variants=[name],
            lock=lock_path if lock_path.is_file() else None,
            runner=runner,
            require=require,
            allow_unbound=True,
            attestations=records,
        )
        return [dict(reason) for reason in found.reasons]

    try:
        if not record.is_file():
            return gather(())
        try:
            return gather((record,))
        except ValidationError:
            return gather(())  # a record of another artifact: as if there were none
    except TdxError as exc:
        message = str(exc.args[0]) if exc.args else str(exc)
        return [_reason("evidence-unavailable", name, message)]


def _reason(code: str, subject: str, message: str) -> dict[str, Any]:
    return {"severity": "incomplete", "code": code, "subject": subject, "message": message}


def _in_progress(
    loaded: RecipeFile, releases: Path, build: Path, call: Invocation
) -> tuple[StatusItem, ...]:
    """One ``pending`` line per unfinished release of the recipe under *releases*."""
    items: list[StatusItem] = []
    for row in releases_in_progress(releases, loaded.recipe.name):
        resume = _resume_command(row, build, call)
        codes = ", ".join(row["reasons"])
        if row["failed"] is not None:
            where = f"stopped at {row['failed']}"
        elif row["verdict"] is not None:
            where = f"verdict {row['verdict']}" + (f" ({codes})" if codes else "")
        else:
            where = f"{len(row['completed'])} stages kept"
        detail = f"{row['name']}: in progress at {row['path']}, {where}  resume with: {resume}"
        items.append(StatusItem("release", "pending", detail, {**row, "resume": resume}))
    return tuple(items)


def _resume_command(row: Mapping[str, Any], build: Path, call: Invocation) -> str:
    """The ``tundravm release`` command that resumes the release of *row*: it inherits every
    option its spec recorded, so only the paths and a backend other than this one's."""
    spec: Mapping[str, Any] = row["spec"]
    words = ["tundravm", "release", call.recipe, "--version", str(row["version"])]
    if call.releases is not None:
        words += ["--releases", str(call.releases)]
    recorded = spec.get("build")
    if isinstance(recorded, str) and Path(recorded) != build.resolve():
        words += ["--build", recorded]
    elif call.build is not None:
        words += ["--build", str(call.build)]
    if call.lockfile is not None:
        words += ["--lockfile", str(call.lockfile)]
    backend = spec.get("backend")
    kind = backend.get("name") if isinstance(backend, Mapping) else None
    if isinstance(kind, str) and kind != call.backend:
        words += ["--backend", kind]
    return " ".join(word for word in words if word)


def _backend(loaded: RecipeFile, runner: ProbeRunner) -> StatusItem:
    backend = loaded.backend
    if backend is None:
        data: dict[str, object] = {"name": None, "ok": 0, "missing": [], "optional_missing": []}
        detail = "no `backend` in the recipe file; bake with --backend KIND"
        return StatusItem("backend", "n/a", detail, data)
    found = [
        (requirement, probe(requirement, runner)[0]) for requirement in requirements_of(backend)
    ]
    ok = sum(1 for _, present in found if present)
    missing = [r.tool for r, present in found if not present and not r.optional]
    optional = [r.tool for r, present in found if not present and r.optional]
    data = {"name": backend.name, "ok": ok, "missing": missing, "optional_missing": optional}
    if not found:
        return StatusItem("backend", "ok", f"{backend.name}: no host tools needed", data)
    parts = [f"{backend.name}: {ok} tool{'' if ok == 1 else 's'} ok"]
    if missing:
        parts.append(f"missing {', '.join(missing)}")
    if optional:
        parts.append(f"optional missing {', '.join(optional)}")
    return StatusItem("backend", "missing" if missing else "ok", ", ".join(parts), data)


def _next(status: ProjectStatus, call: Invocation, tree: Path) -> str:
    return " ".join(_next_command(status, call, tree).split())


def _next_command(status: ProjectStatus, call: Invocation, tree: Path) -> str:
    variants = "".join(f" --variant {name}" for name in call.variants)
    build_flag = "" if call.build is None else f" --build {call.build}"
    lock_flag = "" if call.lockfile is None else f" --lockfile {call.lockfile}"
    if status.lint.verdict == "error":
        return f"tundravm lint {call.recipe}{variants}"
    if status.lock.verdict != "ok":
        path = "" if call.lockfile is None else f" --lockfile {call.lockfile}"
        return f"tundravm lock {call.recipe}{variants}{path}"
    if overall(status.sources) in ("missing", "stale"):
        modified = any("modified" in item.data for item in status.sources)
        force = " --force" if modified else ""
        return f"tundravm fetch {call.recipe}{variants}{build_flag}{lock_flag}{force}"
    if status.tree.verdict in ("missing", "stale"):
        tree_flag = f" --tree {tree}" if call.tree_flag else ""
        return f"tundravm compile {call.recipe}{variants}{tree_flag}{lock_flag}"
    if overall(status.artifacts) in ("missing", "stale"):
        if status.backend.verdict == "missing":
            return f"tundravm doctor {call.recipe}"
        return f"tundravm bake {call.recipe}{variants}{build_flag}{lock_flag}"
    return _release_next(status, call)


def _release_next(status: ProjectStatus, call: Invocation) -> str:
    """Past a current bake: a second build, ``measure``, ``attest``, then ``release``."""
    variants = "".join(f" --variant {name}" for name in call.variants)
    build_flag = "" if call.build is None else f" --build {call.build}"
    lock_flag = "" if call.lockfile is None else f" --lockfile {call.lockfile}"
    build = status.manifest.parent
    codes = {
        str(item.data["variant"]): list(dict.fromkeys(str(r["code"]) for r in _reasons(item)))
        for item in status.releases
        if item.verdict == "blocked"
    }
    every = {code for found in codes.values() for code in found}
    simulated = {str(item.data.get("variant")) for item in status.artifacts if _simulated(item)}
    if "lock-missing" in every:
        return f"tundravm lock {call.recipe}{variants}{lock_flag}"
    if every & (_REBAKE | _REPRODUCE):
        twice = bool(every & _REPRODUCE) or "reproducible" in status.require
        again = " --verify-reproducible" if twice else ""
        return f"tundravm bake {call.recipe}{variants}{build_flag}{lock_flag}{again}"
    for name, found in codes.items():
        if name in simulated:
            continue
        policy = build / name / "policy.json"
        if set(found) & _MEASURE:
            return f"tundravm measure{build_flag} --variant {name} --export-policy {policy}"
        if set(found) & _ATTEST:
            record = build / name / ATTESTATION_NAME
            return (
                "tundravm attest --issuer ISSUER --validator VALIDATOR "
                f"--policy {policy} --record {record}"
            )
    if status.in_progress:
        return str(status.in_progress[0].data["resume"])
    if codes:
        listed = "; ".join(f"{name} ({', '.join(found)})" for name, found in codes.items())
        if any(name in simulated and set(found) & _NEEDS_REAL for name, found in codes.items()):
            return f"{BLOCKED}: {listed}: simulated artifacts need a bake with a real --backend"
        require = ",".join(status.require) or "''"
        return f"{BLOCKED}: {listed}: tundravm evidence --require {require} says why"
    ready = [item for item in status.releases if item.verdict == "ok"]
    if all(item.data["released"] for item in ready):
        return UP_TO_DATE
    named = "".join(f" --variant {item.data['variant']}" for item in ready) if variants else ""
    releases = "" if call.releases is None else f" --releases {call.releases}"
    require = ",".join(status.require) or "''"
    return (
        f"tundravm release {call.recipe} --version VERSION{named} --require {require}"
        f"{releases}{build_flag}{lock_flag}"
    )


def _reasons(item: StatusItem) -> list[Mapping[str, Any]]:
    reasons = item.data.get("reasons")
    return [r for r in reasons if isinstance(r, Mapping)] if isinstance(reasons, list) else []


def _simulated(item: StatusItem) -> bool:
    return item.data.get("simulated") is True


# ── rendering ────────────────────────────────────────────────────────────


def render_status(status: ProjectStatus, fmt: str) -> str:
    """*status* as ``text`` (one line per item), ``json`` or ``markdown`` (a table per section)."""
    if fmt == "json":
        return json.dumps(status.to_dict(), indent=2, sort_keys=True)
    if fmt == "markdown":
        return _markdown(status)
    lines = [
        f"{item.label:<{_LABEL_WIDTH}}{item.verdict:<{_VERDICT_WIDTH}}{item.detail}"
        for item in status.items()
    ]
    lines.append(f"next: {status.next}")
    return "\n".join(lines)


_SECTIONS = (
    "Recipe",
    "Lint",
    "Lock",
    "Sources",
    "Tree",
    "Artifacts",
    "Manifests",
    "Backend",
    "Release",
)


def _markdown(status: ProjectStatus) -> str:
    sources = status.sources or (StatusItem("source", "n/a", "no source builds or kernels"),)
    groups: tuple[tuple[StatusItem, ...], ...] = (
        (status.recipe,),
        (status.lint,),
        (status.lock,),
        sources,
        (status.tree,),
        status.artifacts,
        status.manifests,
        (status.backend,),
        (*status.releases, *status.in_progress),
    )
    blocks = [f"# tundravm status: `{status.recipe.data['path']}`"]
    for heading, items in zip(_SECTIONS, groups, strict=True):
        if not items:
            continue
        rows = [(md_cell(item.verdict), md_cell(item.detail, code=False)) for item in items]
        blocks.append(f"## {heading}\n\n{md_table(('Verdict', 'Detail'), rows)}")
    command = status.next.startswith("tundravm ")
    blocks.append(f"**Next:** {md_cell(status.next, code=command)}")
    return "\n\n".join(blocks)


__all__ = [
    "ATTESTATION_NAME",
    "BLOCKED",
    "DEFAULT_REQUIRE",
    "UP_TO_DATE",
    "VERDICTS",
    "Invocation",
    "ProjectStatus",
    "StatusItem",
    "Verdict",
    "overall",
    "project_status",
    "render_status",
]
