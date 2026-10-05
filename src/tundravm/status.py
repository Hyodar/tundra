"""``tundravm status``: where a recipe project stands, read-only and network-free.

One :class:`StatusItem` per thing the lifecycle produces (the lockfile, each
source checkout, the mkosi tree, each baked artifact) plus the recipe, its lint
summary and the build backend's host tools, each with a verdict. ``next`` is the
single most useful command to run next.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from ._source import SOURCES_DIRNAME, is_fetched
from .check import summarize
from .declarative._lowered import Lowered
from .declarative.lifecycle import (
    INPROCESS,
    MANIFEST_KEY,
    Artifact,
    Lock,
    ProbeRunner,
    check_report,
    compile_image,
    probe,
    read_artifacts,
    read_lock,
    requirements_of,
    using_lock,
)
from .diff import diff_against
from .errors import TdxError
from .formats import md_cell, md_table
from .lockfile import lock_variants
from .models import BAKE_RESULT_FILENAME
from .recipe import RecipeFile

Verdict = Literal["ok", "stale", "missing", "n/a", "error"]
VERDICTS: tuple[Verdict, ...] = ("ok", "stale", "missing", "n/a", "error")
"""``error`` is only the lint verdict: the recipe has error diagnostics."""

UP_TO_DATE = "everything is up to date"
TREE_DIRNAME = "mkosi"

_RANK: dict[Verdict, int] = {"n/a": 0, "ok": 1, "stale": 2, "missing": 3, "error": 4}
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
    out: Path | None = None
    lockfile: Path | None = None
    variants: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectStatus:
    recipe: StatusItem
    lint: StatusItem
    lock: StatusItem
    sources: tuple[StatusItem, ...]
    tree: StatusItem
    manifest: Path
    artifacts: tuple[StatusItem, ...]
    backend: StatusItem
    next: str

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
            self.backend,
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
            "backend": self.backend.to_dict(),
            "next": self.next,
        }


def overall(items: Sequence[StatusItem]) -> Verdict:
    """The worst verdict of *items* (``n/a`` when there are none)."""
    return max((item.verdict for item in items), key=_RANK.__getitem__, default="n/a")


def project_status(
    loaded: RecipeFile,
    variants: Sequence[str],
    *,
    out: Path,
    lock_path: Path,
    runner: ProbeRunner,
    invocation: Invocation,
) -> ProjectStatus:
    """Report *variants* of *loaded* against the build output in *out*; never writes."""
    img = loaded.lowered()
    names = tuple(variants)
    lock, lock_item = _lock(img, names, lock_path)
    sources = _sources(img, names, lock, out)
    tree = _tree(img, names, lock, out / TREE_DIRNAME)
    manifest = out / BAKE_RESULT_FILENAME
    artifacts = _artifacts(img, names, lock, manifest)
    backend = _backend(loaded, runner)
    lint = _lint(loaded, names)
    status = ProjectStatus(
        recipe=_recipe(loaded, img, names, invocation.recipe),
        lint=lint,
        lock=lock_item,
        sources=sources,
        tree=tree,
        manifest=manifest,
        artifacts=artifacts,
        backend=backend,
        next="",
    )
    return replace(status, next=_next(status, invocation, out))


def _recipe(loaded: RecipeFile, img: Lowered, names: tuple[str, ...], shown: str) -> StatusItem:
    digest = img.select(names).digest()
    recipe = loaded.recipe
    data = {
        "path": shown,
        "digest": digest,
        "variants": list(names),
        "dialect": img.mkosi.dialect,
        "base": recipe.base,
        "snapshot": recipe.snapshot,
    }
    detail = (
        f"{shown}  digest {digest[:12]}  variants {', '.join(names)}  "
        f"dialect {img.mkosi.dialect}  base {recipe.base}"
        + (f"  snapshot {recipe.snapshot}" if recipe.snapshot else "")
    )
    return StatusItem("recipe", "ok", detail, data)


def _lint(loaded: RecipeFile, names: tuple[str, ...]) -> StatusItem:
    counts = summarize(check_report(loaded.recipe, loaded.image, variants=names))
    detail = ", ".join(f"{count} {level}" for level, count in counts.items())
    verdict: Verdict = "error" if counts["errors"] else "ok"
    return StatusItem("lint", verdict, detail, dict(counts))


def _lock(img: Lowered, names: tuple[str, ...], path: Path) -> tuple[Lock | None, StatusItem]:
    scoped = img.select(names)
    builds = scoped.lock_sources()
    data: dict[str, object] = {"path": str(path), "present": path.is_file()}
    if not path.is_file():
        data.update(up_to_date=False, drift=[], unpinned=sorted(builds), version=None)
        return None, StatusItem("lock", "missing", f"{path}: no lockfile", data)
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
    img: Lowered, names: tuple[str, ...], lock: Lock | None, out: Path
) -> tuple[StatusItem, ...]:
    builds = img.select(names).lock_sources()
    pins = {} if lock is None else {f.name: f for f in lock.lockfile.fetches if f.name is not None}
    root = out / SOURCES_DIRNAME
    simulated = img.backend is not None and img.backend.name == INPROCESS
    items: list[StatusItem] = []
    for name, build in builds.items():
        pin = build.pin_from(pins)
        data: dict[str, object] = {"name": name, "pin": pin, "path": None, "fetched": False}
        if not img.fetches_sources:
            detail = f"{name}: fetched inside the build ({img.mkosi.dialect})"
            items.append(StatusItem("source", "n/a", detail, data))
            continue
        if pin is None:
            detail = f"{name}: not pinned, so no checkout to check"
            items.append(StatusItem("source", "n/a" if simulated else "missing", detail, data))
            continue
        path = root / build.pin_dir(pin)
        data["path"] = str(path)
        others = _other_checkouts(root, name, path)
        if is_fetched(path, pin):
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
    img: Lowered, names: tuple[str, ...], lock: Lock | None, manifest: Path
) -> tuple[StatusItem, ...]:
    if not manifest.is_file():
        return tuple(
            StatusItem(
                "artifact", "missing", f"{name}: not baked (no {manifest})", {"variant": name}
            )
            for name in names
        )
    try:
        artifacts = read_artifacts(manifest)
        extra = json.loads(manifest.read_text(encoding="utf-8")).get(MANIFEST_KEY) or {}
    except (TdxError, OSError, ValueError) as exc:
        detail = f"{manifest} is unreadable: {exc}"
        return tuple(StatusItem("artifact", "stale", detail, {"variant": n}) for n in names)
    lockfile = extra.get("lockfile") if isinstance(extra, Mapping) else None
    current = _current_digests(img, lock, artifacts)
    items: list[StatusItem] = []
    for name in names:
        baked = [artifact for artifact in artifacts if artifact.variant == name]
        if not baked:
            detail = f"{name}: not in {manifest}"
            items.append(StatusItem("artifact", "missing", detail, {"variant": name}))
        for artifact in baked:
            items.append(_artifact(artifact, current, lockfile))
    return tuple(items)


def _current_digests(
    img: Lowered, lock: Lock | None, artifacts: Sequence[Artifact]
) -> tuple[str | None, set[str]]:
    """The tree digest the baked variants compile to now, and the recipe digests to accept."""
    baked = tuple(sorted({artifact.variant for artifact in artifacts}))
    declared = set(img.state.profiles)
    if not baked or not set(baked) <= declared:
        return None, set()
    try:
        tree = compile_image(img, baked, locked=lock).digest
    except TdxError:
        tree = None
    scopes = {tuple(sorted(declared)), baked}
    if lock is not None and set(lock_variants(lock.lockfile)) <= declared:
        scopes.add(lock_variants(lock.lockfile))
    recipes = {img.select(scope).digest() for scope in scopes}
    return tree, recipes


def _artifact(
    artifact: Artifact, current: tuple[str | None, set[str]], lockfile: object
) -> StatusItem:
    tree, recipes = current
    path = artifact.path
    size = path.stat().st_size if path.is_file() else None
    recipe_ok = None if not artifact.recipe_digest else artifact.recipe_digest in recipes
    tree_ok = None if not artifact.tree_digest else artifact.tree_digest == tree
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
    }
    parts = [f"{artifact.variant}/{artifact.target}", str(path)]
    if size is None:
        parts.append("file is gone")
    else:
        parts.extend((_size(size), f"sha256 {artifact.sha256[:12]}"))
    if artifact.simulated:
        parts.append("simulated")
    parts.append(f"lock {lockfile}" if isinstance(lockfile, str) else "unpinned")
    changed = [what for what, ok in (("recipe", recipe_ok), ("tree", tree_ok)) if ok is False]
    if changed:
        parts.append(f"{' and '.join(changed)} changed since the bake")
    verdict: Verdict = "missing" if size is None else ("stale" if changed else "ok")
    return StatusItem("artifact", verdict, "  ".join(parts), data)


def _size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{size} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


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


def _next(status: ProjectStatus, call: Invocation, out: Path) -> str:
    variants = "".join(f" --variant {name}" for name in call.variants)
    out_flag = "" if call.out is None else f" --out {call.out}"
    lock_flag = "" if call.lockfile is None else f" --lockfile {call.lockfile}"
    if status.lint.verdict == "error":
        return f"tundravm lint {call.recipe}{variants}"
    if status.lock.verdict != "ok":
        path = "" if call.lockfile is None else f" --path {call.lockfile}"
        return f"tundravm lock {call.recipe}{variants}{path}"
    if overall(status.sources) in ("missing", "stale"):
        return f"tundravm fetch {call.recipe}{variants}{out_flag}{lock_flag}"
    if status.tree.verdict in ("missing", "stale"):
        return f"tundravm compile {call.recipe}{variants} --out {out / TREE_DIRNAME}{lock_flag}"
    if overall(status.artifacts) in ("missing", "stale"):
        if status.backend.verdict == "missing":
            return f"tundravm doctor {call.recipe}"
        return f"tundravm bake {call.recipe}{variants}{out_flag}{lock_flag}"
    return UP_TO_DATE


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


_SECTIONS = ("Recipe", "Lint", "Lock", "Sources", "Tree", "Artifacts", "Backend")


def _markdown(status: ProjectStatus) -> str:
    sources = status.sources or (StatusItem("source", "n/a", "no source builds or kernels"),)
    groups: tuple[tuple[StatusItem, ...], ...] = (
        (status.recipe,),
        (status.lint,),
        (status.lock,),
        sources,
        (status.tree,),
        status.artifacts,
        (status.backend,),
    )
    blocks = [f"# tundravm status: `{status.recipe.data['path']}`"]
    for heading, items in zip(_SECTIONS, groups, strict=True):
        rows = [(md_cell(item.verdict), md_cell(item.detail, code=False)) for item in items]
        blocks.append(f"## {heading}\n\n{md_table(('Verdict', 'Detail'), rows)}")
    blocks.append(f"**Next:** {md_cell(status.next, code=status.next != UP_TO_DATE)}")
    return "\n\n".join(blocks)


__all__ = [
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
