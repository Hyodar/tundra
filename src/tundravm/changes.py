"""What changed since a release: a previous evidence bundle against the current recipe and lock.

:func:`changes` reads the evidence ``tundravm evidence`` wrote for an earlier bake
(the bundle, the directory, or a release directory holding it) and compares it
with the recipe and lockfile as they are now: the recipe and per-variant tree
digests, the lockfile's section digests, the source pins, the declared packages
(with the recipe's), the installed packages (the evidence's SBOMs with those of a
current bake), the emitted files of each variant's compiled tree (from the
evidence's ``tree.json``, each changed file annotated with the declarations
that produce it) and the measured registers. A comparison one side has no data
for is ``unknown``, never a list of removals. The answer is a
:class:`ChangeReport`, rendered as text, JSON or Markdown for a pull request.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .declarative._lowered import Lowered
from .declarative.bom import sbom, spdx_packages
from .declarative.lifecycle import (
    Artifact,
    Lock,
    Tree,
    compile_image,
    read_artifacts,
    read_lock,
    using_lock,
    variant_tree_digest,
)
from .declarative.lower import lower
from .declarative.model import Recipe
from .errors import TdxError, ValidationError
from .evidence import (
    INDEX_NAME,
    TREE_SCHEMA,
    _compiled_tree,
    _recipe_digests,
    declared_packages,
    tree_rows,
    tree_rows_digest,
)
from .formats import md_cell, md_table
from .lockfile import describe_change, parse_lockfile, section_digests, section_values
from .lockfile.drift import _section_variant
from .lockfile.model import Lockfile
from .measure.policy import accepted, read_policy
from .verify import read_evidence

CHANGES_SCHEMA = "tundravm.changes/2"
"""The ``schema`` key of :meth:`ChangeReport.to_dict` (``changes --format json``)."""
ChangeKind = Literal["added", "removed", "changed"]
ComparisonState = Literal["compared", "unknown"]
ChangesFormat = Literal["text", "json", "markdown"]
CHANGES_FORMATS: tuple[ChangesFormat, ...] = ("text", "json", "markdown")
SECTIONS: tuple[tuple[str, str], ...] = (
    ("recipe", "Recipe and tree"),
    ("lockfile", "Lockfile sections"),
    ("pins", "Source pins"),
    ("packages", "Declared packages"),
    ("installed", "Installed packages"),
    ("files", "Emitted files"),
    ("measurements", "Measurements"),
)
"""Each section of a report and its title, in report order."""
COMPARED = ("packages", "installed", "measurements")
"""The per-variant sections a :class:`Comparison` says were compared or are ``unknown``."""
RELEASE_INDEX = "release.json"
"""The index of a release directory, which ``--since`` takes for the evidence it holds."""
RELEASE_BUNDLE = "evidence.tar.gz"
"""The evidence bundle in a release directory."""
ORIGIN_LIMIT = 25
"""At most this many changed files are explained (each compiles the variant once)."""
STAGE_DIRS = ("mkosi.extra", "mkosi.skeleton")
_MARKS = {"added": "+", "removed": "-", "changed": "~"}


@dataclass(frozen=True, slots=True)
class Change:
    """One difference: its ``section`` (:data:`SECTIONS`), ``change``, ``subject`` and the
    ``old`` and ``new`` values; ``detail`` says more, ``origins`` name the declarations
    behind an emitted file."""

    section: str
    change: ChangeKind
    subject: str
    variant: str | None = None
    old: str | None = None
    new: str | None = None
    detail: str | None = None
    origins: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "section": self.section,
            "change": self.change,
            "subject": self.subject,
            "variant": self.variant,
            "old": self.old,
            "new": self.new,
            "detail": self.detail,
            "origins": list(self.origins),
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    """Whether a variant's ``section`` (:data:`COMPARED`) was ``compared`` or is ``unknown``
    because one side lacks the data; ``reason`` says which side and why."""

    section: str
    variant: str
    state: ComparisonState
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "section": self.section,
            "variant": self.variant,
            "state": self.state,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ChangeReport:
    """What :func:`changes` found since the evidence at ``since``."""

    since: str
    created: str | None
    """When the previous evidence was made."""
    recipe: str | None
    variants: tuple[str, ...]
    changes: tuple[Change, ...]
    notes: tuple[str, ...] = ()
    comparisons: tuple[Comparison, ...] = ()
    """For each variant, whether its declared and installed packages and its registers were
    compared; an ``unknown`` one reports no change."""

    @property
    def unknown(self) -> tuple[Comparison, ...]:
        """The comparisons one side had no data for."""
        return tuple(c for c in self.comparisons if c.state == "unknown")

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def section(self, name: str) -> tuple[Change, ...]:
        return tuple(change for change in self.changes if change.section == name)

    def counts(self) -> dict[str, int]:
        return {name: len(self.section(name)) for name, _ in SECTIONS}

    def to_dict(self) -> dict[str, object]:
        """What ``changes --format json`` prints; ``schema`` is ``tundravm.changes/2``."""
        return {
            "schema": CHANGES_SCHEMA,
            "since": self.since,
            "created": self.created,
            "recipe": self.recipe,
            "variants": list(self.variants),
            "counts": self.counts(),
            "changes": [change.to_dict() for change in self.changes],
            "comparisons": [comparison.to_dict() for comparison in self.comparisons],
            "notes": list(self.notes),
        }

    def render(self, format: ChangesFormat = "text") -> str:
        """``text`` (grouped by section with counts), ``json`` or ``markdown`` (for a PR)."""
        if format == "json":
            return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        if format == "markdown":
            return _markdown(self)
        return _text(self)


def changes(
    recipe_or_lowered: Recipe | Lowered,
    *,
    since: Path,
    lock: Lock | Path | None = None,
    variants: Sequence[str] | None = None,
    build: Path | None = None,
) -> ChangeReport:
    """What changed between the evidence at *since* and the recipe and lockfile as they are now.

    *since* is an evidence bundle or directory, or a release directory (``release.json``
    beside ``evidence.tar.gz``). *lock* (a :class:`Lock` or a path) defaults to the
    recipe's lockfile when it exists. *variants* defaults to the evidence's variants
    the recipe still declares; a declared variant the evidence's lockfile already
    held but that was not baked is noted, not reported as added.

    Declared packages are compared with declared ones (the evidence's
    ``declared_packages`` with the recipe's) and installed with installed (the
    evidence's SBOMs that hold mkosi's inventory with those of the bake in *build*).
    *build* is used only when its recipe, tree and lockfile are the current ones;
    its SBOMs only when bound to it, and its ``<variant>/policy.json`` (the current
    registers) only when it measured one of the variant's artifacts. A comparison
    one side has no data for is ``unknown`` in :attr:`ChangeReport.comparisons`, with
    the reason. Nothing is written.
    """
    recipe = recipe_or_lowered if isinstance(recipe_or_lowered, Recipe) else None
    img = lower(recipe) if recipe is not None else recipe_or_lowered
    assert isinstance(img, Lowered)
    since = _evidence_path(Path(since))
    files, _ = read_evidence(since)
    index = _index(files, since)
    notes: list[str] = []
    found: list[Change] = []
    compared: list[Comparison] = []
    _note_unverified(files, index, notes)
    declared = tuple(img.state.profiles)
    previous = _previous_lock(files, notes)
    locked_then = () if previous is None else _lock_variant_names(previous)
    names = _variants(index, declared, variants, found, locked_then, notes)
    current = _current_lock(img, lock, notes)
    found.extend(_recipe_changes(index, img))
    tree = _current_tree(img, names, current, notes)
    found.extend(_lock_changes(previous, current, img, names, declared, notes))
    found.extend(_pin_changes(previous, current, notes))
    baked, unused = _current_build(img, build, current)
    if build is not None and unused is not None:
        notes.append(f"the bake in {build} is not used: {unused}")
    for name in names:
        found.extend(_declared_changes(name, index, img, compared))
        found.extend(_installed_changes(name, files, index, baked, unused, current, compared))
    budget = [ORIGIN_LIMIT]
    with using_lock(img, current) as pinned:
        for name in names:
            found.extend(_file_changes(name, files, tree, recipe, pinned, budget, notes))
    if budget[0] < 0:
        notes.append(f"only the first {ORIGIN_LIMIT} changed files are explained")
    for name in names:
        found.extend(_register_changes(name, files, index, build, baked, unused, compared))
    order = {name: n for n, (name, _) in enumerate(SECTIONS)}
    ordered = sorted(found, key=lambda c: order[c.section])
    recipe_entry = index.get("recipe") or {}
    return ChangeReport(
        since=str(since),
        created=index.get("created"),
        recipe=recipe.name if recipe is not None else recipe_entry.get("name"),
        variants=names,
        changes=tuple(ordered),
        notes=tuple(dict.fromkeys(notes)),
        comparisons=tuple(compared),
    )


# ── the two sides ────────────────────────────────────────────────────────


def _evidence_path(since: Path) -> Path:
    """*since*, or the evidence bundle of the release directory it names."""
    if not (since.is_dir() and (since / RELEASE_INDEX).is_file()):
        return since
    bundle = since / RELEASE_BUNDLE
    if not bundle.is_file():
        raise ValidationError(
            f"The release directory {since} holds no {RELEASE_BUNDLE}.",
            hint="Finish the release (`tundravm release`), or pass its evidence bundle.",
            context={"path": str(since)},
        )
    return bundle


def _index(files: Mapping[str, bytes], since: Path) -> dict[str, Any]:
    try:
        index = json.loads(files[INDEX_NAME])
    except ValueError as exc:
        raise ValidationError(
            f"{since}: {INDEX_NAME} is not JSON ({exc}).",
            hint="Run `tundravm verify` on it; pass evidence `tundravm evidence` wrote.",
        ) from exc
    if not isinstance(index, dict) or not isinstance(index.get("variants"), dict):
        raise ValidationError(
            f"{since}: {INDEX_NAME} is not an evidence index.",
            hint="Run `tundravm verify` on it; pass evidence `tundravm evidence` wrote.",
        )
    return index


def _note_unverified(
    files: Mapping[str, bytes], index: Mapping[str, Any], notes: list[str]
) -> None:
    listed = index.get("members") or {}
    differ = [
        name
        for name, entry in listed.items()
        if name not in files or hashlib.sha256(files[name]).hexdigest() != entry.get("sha256")
    ]
    if differ:
        notes.append(
            f"the evidence does not verify ({', '.join(sorted(differ))} differ from its index): "
            "run `tundravm verify` before trusting this comparison"
        )


def _variants(
    index: Mapping[str, Any],
    declared: Sequence[str],
    wanted: Sequence[str] | None,
    found: list[Change],
    locked_then: Sequence[str],
    notes: list[str],
) -> tuple[str, ...]:
    """*wanted*, else the evidence's variants the recipe still declares. A variant only in
    the evidence is removed; one only declared is added unless the evidence's lockfile
    (*locked_then*) holds it, which means it existed but was not baked: a note."""
    previous = sorted(index["variants"])
    if wanted is not None:
        unknown = [name for name in wanted if name not in declared]
        if unknown:
            raise ValidationError(
                f"Unknown variant(s): {', '.join(unknown)}.",
                hint=f"Declared variants: {', '.join(declared)}",
            )
        return tuple(dict.fromkeys(wanted))
    for name in previous:
        if name not in declared:
            found.append(Change("recipe", "removed", f"variant {name}", variant=name))
    for name in sorted(declared):
        if name in previous:
            continue
        if name in locked_then:
            notes.append(f"variant {name} is not in the evidence (it was not baked): not compared")
        else:
            found.append(
                Change(
                    "recipe", "added", f"variant {name}", variant=name, detail="not in the evidence"
                )
            )
    return tuple(name for name in previous if name in declared)


def _current_lock(img: Lowered, given: Lock | Path | None, notes: list[str]) -> Lock | None:
    if isinstance(given, Lock):
        return given
    if given is not None:
        return read_lock(Path(given))
    if img.lock_path.is_file():
        return read_lock(img.lock_path)
    notes.append(f"no current lockfile ({img.lock_path}): source pins are not compared")
    return None


def _previous_lock(files: Mapping[str, bytes], notes: list[str]) -> Lockfile | None:
    data = files.get("tundravm.lock")
    if data is None:
        notes.append("the evidence holds no lockfile: lockfile sections and pins are not compared")
        return None
    try:
        return parse_lockfile(data.decode("utf-8", errors="replace"))
    except TdxError as exc:
        notes.append(f"the evidence's lockfile does not parse ({exc}): it is not compared")
        return None


def _current_tree(
    img: Lowered, names: Sequence[str], locked: Lock | None, notes: list[str]
) -> Tree | None:
    if not names:
        return None
    try:
        return compile_image(img, names, locked=locked)
    except TdxError as exc:
        notes.append(f"the recipe does not compile now ({exc}): emitted files are not compared")
        return None


def _json(data: bytes | None) -> Any:
    if data is None:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


# ── sections ─────────────────────────────────────────────────────────────


def _recipe_changes(index: Mapping[str, Any], img: Lowered) -> list[Change]:
    old = (index.get("recipe") or {}).get("digest")
    new = img.select(tuple(sorted(img.state.profiles))).digest()
    if old == new:
        return []
    return [Change("recipe", "changed", "recipe digest", old=old, new=new)]


def _lock_changes(
    previous: Lockfile | None,
    current: Lock | None,
    img: Lowered,
    names: Sequence[str],
    declared: Sequence[str],
    notes: list[str],
) -> list[Change]:
    if previous is None:
        return []
    if current is not None:
        new_sections = dict(current.lockfile.sections)
        new_values = section_values(current.lockfile.recipe)
    else:
        payload = img.payload()
        new_sections = section_digests(payload)
        new_values = section_values(payload)
        notes.append("lockfile sections are compared with the recipe as it is now (no lockfile)")
    old_sections = dict(previous.sections)
    old_values = section_values(previous.recipe)
    known = tuple({*declared, *_lock_variant_names(previous)})

    def relevant(section: str) -> bool:
        owner = _section_variant(section, known)
        return owner is None or owner in names

    found: list[Change] = []
    for section in sorted({*old_sections, *new_sections}):
        if not relevant(section):
            continue
        old, new = old_sections.get(section), new_sections.get(section)
        if old == new:
            continue
        kind: ChangeKind = "changed" if old and new else ("added" if new else "removed")
        detail = (
            describe_change(old_values.get(section), new_values.get(section))
            if kind == "changed"
            else None
        )
        found.append(Change("lockfile", kind, section, old=old, new=new, detail=detail))
    return found


def _lock_variant_names(lockfile: Lockfile) -> tuple[str, ...]:
    entries = lockfile.recipe.get("profiles")
    return tuple(str(name) for name in entries) if isinstance(entries, dict) else ()


def _pin_changes(previous: Lockfile | None, current: Lock | None, notes: list[str]) -> list[Change]:
    if previous is None or current is None:
        return []
    old = {fetch.name or fetch.source: fetch for fetch in previous.fetches}
    new = {fetch.name or fetch.source: fetch for fetch in current.lockfile.fetches}
    found: list[Change] = []
    for key in sorted({*old, *new}):
        before, after = old.get(key), new.get(key)
        if before is not None and after is not None:
            if (before.digest, before.ref, before.source) == (
                after.digest,
                after.ref,
                after.source,
            ):
                continue
            detail = _ref(before.ref, after.ref)
            if before.source != after.source:
                detail = f"{detail}; url {before.source} -> {after.source}"
            found.append(
                Change("pins", "changed", key, old=before.digest, new=after.digest, detail=detail)
            )
        elif after is not None:
            found.append(
                Change("pins", "added", key, new=after.digest, detail=_ref(None, after.ref))
            )
        elif before is not None:
            found.append(
                Change("pins", "removed", key, old=before.digest, detail=_ref(before.ref, None))
            )
    return found


def _ref(old: str | None, new: str | None) -> str:
    if old == new or old is None or new is None:
        return f"ref {new or old or '-'}"
    return f"ref {old} -> {new}"


def _current_build(
    img: Lowered, build: Path | None, current: Lock | None
) -> tuple[tuple[Artifact, ...], str | None]:
    """The artifacts of the bake in *build* and ``None``, or none and why that bake does not
    describe the current recipe: another recipe, tree or lockfile."""
    if build is None:
        return (), "no --build DIR"
    try:
        artifacts = read_artifacts(Path(build))
    except TdxError as exc:
        return (), f"it holds no readable bake ({exc})"
    baked = sorted({a.variant for a in artifacts})
    if not artifacts:
        return (), "it holds no artifact"
    if not set(baked) <= set(img.state.profiles):
        return (), f"it bakes variants the recipe no longer declares ({', '.join(baked)})"
    recorded = sorted({a.recipe_digest for a in artifacts})
    if not set(recorded) <= _recipe_digests(img, baked, current):
        return (), f"it was baked from another recipe (digest {recorded[0][:12]})"
    tree = _compiled_tree(img, artifacts, current)
    moved = [
        a
        for a in artifacts
        if tree is None or a.tree_digest != variant_tree_digest(tree, a.variant)
    ]
    if moved:
        return (), f"it was baked from another tree (digest {moved[0].tree_digest[:12]})"
    sha = None if current is None else hashlib.sha256(current.text().encode()).hexdigest()
    if {a.lock_sha256 for a in artifacts} != {sha}:
        return (), "it read another lockfile than the current one"
    return artifacts, None


def _unknown(
    compared: list[Comparison], section: str, name: str, reason: str | None
) -> list[Change]:
    """Record *section* of *name* as compared (*reason* ``None``) or ``unknown``; no change."""
    state: ComparisonState = "compared" if reason is None else "unknown"
    compared.append(Comparison(section, name, state, reason))
    return []


def _package_rows(
    section: str, name: str, old: Mapping[str, str], new: Mapping[str, str]
) -> list[Change]:
    """Packages added, removed and (when both sides have a version) changed."""
    found = [
        Change(section, "added", p, variant=name, new=new[p] or None)
        for p in sorted(new.keys() - old.keys())
    ]
    found += [
        Change(section, "removed", p, variant=name, old=old[p] or None)
        for p in sorted(old.keys() - new.keys())
    ]
    found += [
        Change(section, "changed", p, variant=name, old=old[p], new=new[p])
        for p in sorted(old.keys() & new.keys())
        if old[p] and new[p] and old[p] != new[p]
    ]
    return found


def _declared_changes(
    name: str, index: Mapping[str, Any], img: Lowered, compared: list[Comparison]
) -> list[Change]:
    """The packages the evidence's recipe declared for *name* against the ones declared now."""
    record = index["variants"].get(name) or {}
    old = record.get("declared_packages")
    if not isinstance(old, list):
        reason = "the evidence records no declared packages (made before tundravm.evidence/2)"
        return _unknown(compared, "packages", name, reason)
    _unknown(compared, "packages", name, None)
    new = declared_packages(img, name)
    return _package_rows("packages", name, dict.fromkeys(map(str, old), ""), dict.fromkeys(new, ""))


def _previous_installed(
    name: str, files: Mapping[str, bytes], index: Mapping[str, Any]
) -> dict[str, str] | None:
    """The installed packages the evidence's SBOMs of *name* list, from mkosi's inventory;
    ``None`` when no SBOM holds one."""
    record = index["variants"].get(name) or {}
    found: dict[str, str] | None = None
    for entry in record.get("sbom") or []:
        document = _json(files.get(str(entry.get("member"))))
        if entry.get("complete") is not True or not isinstance(document, dict):
            continue
        found = found if found is not None else {}
        found.update(spdx_packages(document))
    return found


def _current_installed(
    name: str, baked: Sequence[Artifact], unused: str | None, locked: Lock | None
) -> tuple[dict[str, str] | None, str | None]:
    """The packages the current bake installed for *name*, or ``None`` and why not."""
    if unused is not None:
        return None, f"the current side has no usable bake: {unused}"
    found: dict[str, str] | None = None
    for artifact in (a for a in baked if a.variant == name):
        try:
            document = sbom(artifact, lock=locked, allow_unbound=True)
        except TdxError as exc:
            return None, f"the current bake's SBOM of {name}/{artifact.target}: {exc}"
        if not document.bound:
            return None, f"the current bake's SBOM of {name}/{artifact.target} is not bound to it"
        if not document.complete:
            return None, f"the current bake of {name}/{artifact.target} has no mkosi manifest"
        found = found if found is not None else {}
        found.update({c.name: c.version for c in document.packages})
    if found is None:
        return None, f"the current bake has no artifact of {name}"
    return found, None


def _installed_changes(
    name: str,
    files: Mapping[str, bytes],
    index: Mapping[str, Any],
    baked: Sequence[Artifact],
    unused: str | None,
    locked: Lock | None,
    compared: list[Comparison],
) -> list[Change]:
    """The packages the evidence's bake installed against the ones the current bake did."""
    old = _previous_installed(name, files, index)
    if old is None:
        return _unknown(
            compared,
            "installed",
            name,
            "the evidence holds no installed inventory (no SBOM from a mkosi manifest)",
        )
    new, reason = _current_installed(name, baked, unused, locked)
    if new is None:
        return _unknown(compared, "installed", name, reason)
    _unknown(compared, "installed", name, None)
    return _package_rows("installed", name, old, new)


def _file_changes(
    name: str,
    files: Mapping[str, bytes],
    tree: Tree | None,
    recipe: Recipe | None,
    img: Lowered,
    budget: list[int],
    notes: list[str],
) -> list[Change]:
    manifest = _json(files.get(f"variants/{name}/tree.json"))
    if not isinstance(manifest, dict) or manifest.get("schema") != TREE_SCHEMA:
        notes.append(f"{name}: the evidence holds no tree.json: emitted files are not compared")
        return []
    if tree is None:
        return []
    old = {str(row["path"]): row for row in manifest.get("files") or [] if isinstance(row, dict)}
    rows = tree_rows(tree, name)
    new = {str(row["path"]): row for row in rows}
    found: list[Change] = []
    old_digest = manifest.get("variant_digest")
    new_digest = tree_rows_digest(((name, row) for row in rows), tree.layout)
    if old_digest != new_digest:
        found.append(
            Change(
                "recipe", "changed", f"{name} tree", variant=name, old=old_digest, new=new_digest
            )
        )
    for path in sorted({*old, *new}):
        before, after = old.get(path), new.get(path)
        if before is not None and after is not None:
            detail = _file_detail(before, after)
            if detail is None:
                continue
            kind: ChangeKind = "changed"
        elif after is not None:
            kind, detail = "added", _kind(after)
        else:
            kind, detail = "removed", _kind(before or {})
        origins = _origins(name, path, recipe, img, budget) if kind != "removed" else ()
        found.append(
            Change(
                "files",
                kind,
                path,
                variant=name,
                old=None if before is None else before.get("sha256"),
                new=None if after is None else after.get("sha256"),
                detail=detail,
                origins=origins,
            )
        )
    return found


def _kind(row: Mapping[str, Any]) -> str:
    if row.get("type") == "symlink":
        return f"symlink to {row.get('target')}"
    return f"{row.get('type')} {row.get('mode')}"


def _file_detail(before: Mapping[str, Any], after: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    if before.get("type") != after.get("type"):
        parts.append(f"{before.get('type')} -> {after.get('type')}")
    if before.get("sha256") != after.get("sha256") and after.get("type") == "file":
        parts.append("content")
    if before.get("target") != after.get("target") and after.get("type") == "symlink":
        parts.append(f"target {before.get('target')} -> {after.get('target')}")
    if before.get("mode") != after.get("mode"):
        parts.append(f"mode {before.get('mode')} -> {after.get('mode')}")
    return ", ".join(parts) or None


def _origins(
    name: str, path: str, recipe: Recipe | None, img: Lowered, budget: list[int]
) -> tuple[str, ...]:
    stage, _, rest = path.partition("/")
    if stage not in STAGE_DIRS or not rest:
        return ()
    if recipe is None:
        return ()
    budget[0] -= 1
    if budget[0] < 0:
        return ()
    from .explain import explain_why

    try:
        why = explain_why(recipe, name, f"/{rest}", lowered=img)
    except TdxError:
        return ()
    found: list[str] = []
    for entry in why.declarations:
        if not entry.present:
            continue
        last = entry.origins[-1] if entry.origins else None
        if last is None:
            found.append(entry.declaration)
            continue
        chain = " > ".join(fragment for fragment, _ in last.fragments) or last.variant
        found.append(f"{entry.declaration} ({last.action} in {chain})")
    return tuple(found)


def _register_changes(
    name: str,
    files: Mapping[str, bytes],
    index: Mapping[str, Any],
    build: Path | None,
    baked: Sequence[Artifact],
    unused: str | None,
    compared: list[Comparison],
) -> list[Change]:
    record = index["variants"].get(name) or {}
    policy = record.get("policy")
    member = policy.get("member") if isinstance(policy, dict) else None
    old = _registers(_json(files.get(str(member))) if member else None)
    if old is None:
        return _unknown(
            compared, "measurements", name, "the evidence holds no valid measurements policy"
        )
    new, reason = _current_registers(name, build, baked, unused)
    if new is None:
        return _unknown(compared, "measurements", name, reason)
    _unknown(compared, "measurements", name, None)
    return [
        Change(
            "measurements",
            "changed"
            if register in old and register in new
            else ("added" if register in new else "removed"),
            register,
            variant=name,
            old=old.get(register),
            new=new.get(register),
        )
        for register in sorted({*old, *new})
        if old.get(register) != new.get(register)
    ]


def _current_registers(
    name: str, build: Path | None, baked: Sequence[Artifact], unused: str | None
) -> tuple[dict[str, str] | None, str | None]:
    """The registers of ``BUILD/<name>/policy.json`` when it measured one of the current
    bake's artifacts of *name*, else ``None`` and why not."""
    if build is None or unused is not None:
        return None, f"the current side has no usable bake: {unused}"
    path = Path(build) / name / "policy.json"
    payload = _json(path.read_bytes()) if path.is_file() else None
    registers = _registers(payload)
    if registers is None:
        return None, f"{path} is not a valid measurements policy (or is missing)"
    measured = payload.get("artifact") if isinstance(payload, dict) else None
    sha = measured.get("sha256") if isinstance(measured, dict) else None
    if sha not in {a.sha256 for a in baked if a.variant == name}:
        return None, f"{path} measured no artifact of the current bake of {name}"
    return registers, None


def _registers(payload: Any) -> dict[str, str] | None:
    if not isinstance(payload, dict):
        return None
    try:
        checked = read_policy(payload)
    except TdxError:
        return None
    registers = checked.get("registers")
    assert isinstance(registers, dict)
    return {
        name: ",".join(a.value for a in accepted(registers, name)) for name in sorted(registers)
    }


# ── rendering ────────────────────────────────────────────────────────────


def _short(value: str | None) -> str:
    if value is None:
        return "-"
    hexish = len(value) >= 40 and all(c in "0123456789abcdef" for c in value)
    return value[:12] if hexish else value


def _where(change: Change) -> str:
    return f"{change.variant}  " if change.variant and change.section != "recipe" else ""


def _line(change: Change) -> str:
    mark = _MARKS[change.change]
    text = f"  {mark} {_where(change)}{change.subject}"
    if change.section in ("recipe", "pins", "packages", "measurements") and (
        change.old or change.new
    ):
        if change.change == "changed":
            text += f"  {_short(change.old)} -> {_short(change.new)}"
        else:
            text += f"  {_short(change.new if change.change == 'added' else change.old)}"
    if change.detail:
        text += f"  ({change.detail})"
    if change.origins:
        text += f"  from {'; '.join(change.origins)}"
    return text


def _heading(report: ChangeReport) -> str:
    shown = ", ".join(report.variants) or "-"
    return (
        f"changes since evidence {report.created or '-'} ({report.since})  "
        f"recipe {report.recipe or '-'}  variants {shown}"
    )


def _text(report: ChangeReport) -> str:
    counts = report.counts()
    lines = [_heading(report)]
    lines.extend(f"  {name:<12}  {counts[name]}" for name, _ in SECTIONS)
    for name, _ in SECTIONS:
        found = report.section(name)
        if found:
            lines.append(f"{name} ({len(found)}):")
            lines.extend(_line(change) for change in found)
    if not report.changed:
        lines.append("no changes")
    lines.extend(f"unknown: {c.variant} {c.section}: {c.reason}" for c in report.unknown)
    lines.extend(f"note: {note}" for note in report.notes)
    return "\n".join(lines) + "\n"


def _code(value: str | None) -> str:
    return md_cell(None if value is None else _short(value), code=True)


def _rows(name: str, found: Iterable[Change]) -> tuple[tuple[str, ...], list[tuple[str, ...]]]:
    if name == "lockfile":
        header: tuple[str, ...] = ("Change", "Section", "Detail")
        return header, [
            (c.change, md_cell(c.subject, code=True), md_cell(c.detail, code=True)) for c in found
        ]
    if name == "pins":
        header = ("Change", "Source", "Ref", "Before", "After")
        return header, [
            (c.change, md_cell(c.subject, code=True), md_cell(c.detail), _code(c.old), _code(c.new))
            for c in found
        ]
    if name == "files":
        header = ("Change", "Variant", "File", "What", "Declared by")
        return header, [
            (
                c.change,
                md_cell(c.variant, code=True),
                md_cell(c.subject, code=True),
                md_cell(c.detail),
                md_cell("; ".join(c.origins) if c.origins else None),
            )
            for c in found
        ]
    if name == "recipe":
        header = ("Change", "Subject", "Before", "After")
        return header, [(c.change, md_cell(c.subject), _code(c.old), _code(c.new)) for c in found]
    subject = "Register" if name == "measurements" else "Package"
    header = ("Change", "Variant", subject, "Before", "After")
    return header, [
        (
            c.change,
            md_cell(c.variant, code=True),
            md_cell(c.subject, code=True),
            _code(c.old),
            _code(c.new),
        )
        for c in found
    ]


def _markdown(report: ChangeReport) -> str:
    counts = report.counts()
    shown = ", ".join(f"`{name}`" for name in report.variants) or "none"
    total = sum(counts.values())
    parts = [
        f"## Changes since evidence `{report.created or report.since}`",
        f"Recipe `{report.recipe or '-'}`, variants {shown}: "
        + (f"{total} change{'s' if total != 1 else ''}." if total else "no changes."),
        md_table(("Section", "Changes"), [(title, str(counts[name])) for name, title in SECTIONS]),
    ]
    for name, title in SECTIONS:
        found = report.section(name)
        if found:
            header, rows = _rows(name, found)
            parts.append(f"### {title} ({len(found)})")
            parts.append(md_table(header, rows))
    if report.unknown:
        titles = dict(SECTIONS)
        rows = [
            (md_cell(c.variant, code=True), titles[c.section], md_cell(c.reason))
            for c in report.unknown
        ]
        parts.append(f"### Not compared ({len(rows)})")
        parts.append(md_table(("Variant", "Section", "Why"), rows))
    if report.notes:
        parts.append(
            "<details><summary>Notes</summary>\n\n"
            + "\n".join(f"- {md_cell(n)}" for n in report.notes)
            + "\n\n</details>"
        )
    return "\n\n".join(parts) + "\n"


__all__ = [
    "CHANGES_FORMATS",
    "CHANGES_SCHEMA",
    "COMPARED",
    "SECTIONS",
    "Change",
    "ChangeReport",
    "Comparison",
    "changes",
]
