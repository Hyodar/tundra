"""What changed since a release: a previous evidence bundle against the current recipe and lock.

:func:`changes` reads the evidence ``tundravm evidence`` wrote for an earlier bake
(the bundle or the directory) and compares it with the recipe and lockfile as
they are now: the recipe and per-variant tree digests, the lockfile's section
digests, the source pins, the packages (by the SBOMs' versions when both sides
have them), the emitted files of each variant's compiled tree (from the
evidence's ``tree.json``, each changed file annotated with the declarations
that produce it) and the measured registers. The answer is a
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
from .declarative.bom import sbom
from .declarative.lifecycle import (
    Lock,
    Tree,
    compile_image,
    read_artifacts,
    read_lock,
    using_lock,
)
from .declarative.lower import lower
from .declarative.model import Recipe
from .errors import TdxError, ValidationError
from .evidence import INDEX_NAME, TREE_SCHEMA, tree_rows, tree_rows_digest
from .formats import md_cell, md_table
from .lockfile import describe_change, parse_lockfile, section_digests, section_values
from .lockfile.drift import _section_variant
from .lockfile.model import Lockfile
from .measure.policy import read_policy
from .verify import read_evidence

CHANGES_SCHEMA = "tundravm.changes/1"
"""The ``schema`` key of :meth:`ChangeReport.to_dict` (``changes --format json``)."""
ChangeKind = Literal["added", "removed", "changed"]
ChangesFormat = Literal["text", "json", "markdown"]
CHANGES_FORMATS: tuple[ChangesFormat, ...] = ("text", "json", "markdown")
SECTIONS: tuple[tuple[str, str], ...] = (
    ("recipe", "Recipe and tree"),
    ("lockfile", "Lockfile sections"),
    ("pins", "Source pins"),
    ("packages", "Packages"),
    ("files", "Emitted files"),
    ("measurements", "Measurements"),
)
"""Each section of a report and its title, in report order."""
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
class ChangeReport:
    """What :func:`changes` found since the evidence at ``since``."""

    since: str
    created: str | None
    """When the previous evidence was made."""
    recipe: str | None
    variants: tuple[str, ...]
    changes: tuple[Change, ...]
    notes: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def section(self, name: str) -> tuple[Change, ...]:
        return tuple(change for change in self.changes if change.section == name)

    def counts(self) -> dict[str, int]:
        return {name: len(self.section(name)) for name, _ in SECTIONS}

    def to_dict(self) -> dict[str, object]:
        """What ``changes --format json`` prints; ``schema`` is ``tundravm.changes/1``."""
        return {
            "schema": CHANGES_SCHEMA,
            "since": self.since,
            "created": self.created,
            "recipe": self.recipe,
            "variants": list(self.variants),
            "counts": self.counts(),
            "changes": [change.to_dict() for change in self.changes],
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

    *since* is an evidence bundle or directory. *lock* (a :class:`Lock` or a path)
    defaults to the recipe's lockfile when it exists. *variants* defaults to the
    evidence's variants the recipe still declares; a declared variant the evidence's
    lockfile already held but that was not baked is noted, not reported as added.
    *build* is a bake directory of the current recipe whose SBOMs (mkosi manifests)
    give the current package
    versions and whose ``<variant>/policy.json`` the current registers; without it
    the current packages are the declared ones, unversioned. Nothing is written.
    """
    recipe = recipe_or_lowered if isinstance(recipe_or_lowered, Recipe) else None
    img = lower(recipe) if recipe is not None else recipe_or_lowered
    assert isinstance(img, Lowered)
    files, _ = read_evidence(Path(since))
    index = _index(files, Path(since))
    notes: list[str] = []
    found: list[Change] = []
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
    for name in names:
        found.extend(_package_changes(name, files, index, img, current, build, notes))
    budget = [ORIGIN_LIMIT]
    with using_lock(img, current) as pinned:
        for name in names:
            found.extend(_file_changes(name, files, tree, recipe, pinned, budget, notes))
    if budget[0] < 0:
        notes.append(f"only the first {ORIGIN_LIMIT} changed files are explained")
    for name in names:
        found.extend(_register_changes(name, files, index, build, notes))
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
    )


# ── the two sides ────────────────────────────────────────────────────────


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


def _previous_packages(
    name: str, files: Mapping[str, bytes], index: Mapping[str, Any]
) -> dict[str, str] | None:
    record = index["variants"].get(name) or {}
    found: dict[str, str] | None = None
    for entry in record.get("sbom") or []:
        document = _json(files.get(str(entry.get("member"))))
        if not isinstance(document, dict):
            continue
        found = found if found is not None else {}
        found.update(_spdx_packages(document))
    return found


def _spdx_packages(document: Mapping[str, Any]) -> dict[str, str]:
    """Name to version (empty when unknown) of each package the image ``CONTAINS``."""
    contained = {
        r.get("relatedSpdxElement")
        for r in document.get("relationships") or []
        if isinstance(r, dict) and r.get("relationshipType") == "CONTAINS"
    }
    found: dict[str, str] = {}
    for package in document.get("packages") or []:
        if isinstance(package, dict) and package.get("SPDXID") in contained:
            version = str(package.get("versionInfo") or "")
            found[str(package.get("name"))] = "" if version == "NOASSERTION" else version
    return found


def _current_packages(
    name: str, img: Lowered, locked: Lock | None, build: Path | None, notes: list[str]
) -> dict[str, str]:
    if build is not None:
        try:
            baked = [a for a in read_artifacts(Path(build)) if a.variant == name]
            if baked:
                document = sbom(baked[0], lock=locked, allow_unbound=True)
                return {c.name: c.version for c in document.packages}
            notes.append(f"{build} holds no artifact of {name}: its declared packages are used")
        except TdxError as exc:
            notes.append(
                f"{name}: the bake in {build} gives no SBOM ({exc}); declared packages used"
            )
    else:
        notes.append(
            "current packages are the recipe's declared ones, unversioned "
            "(pass --build DIR for the versions a bake installed)"
        )
    profiles = img.payload().get("profiles")
    entry = profiles.get(name) if isinstance(profiles, dict) else None
    declared = entry.get("packages") if isinstance(entry, dict) else None
    return {str(package): "" for package in declared or ()}


def _package_changes(
    name: str,
    files: Mapping[str, bytes],
    index: Mapping[str, Any],
    img: Lowered,
    locked: Lock | None,
    build: Path | None,
    notes: list[str],
) -> list[Change]:
    old = _previous_packages(name, files, index)
    if old is None:
        notes.append(f"{name}: the evidence holds no SBOM: packages are not compared")
        return []
    new = _current_packages(name, img, locked, build, notes)
    found = [
        Change("packages", "added", p, variant=name, new=new[p] or None)
        for p in sorted(new.keys() - old.keys())
    ]
    found += [
        Change("packages", "removed", p, variant=name, old=old[p] or None)
        for p in sorted(old.keys() - new.keys())
    ]
    both = sorted(old.keys() & new.keys())
    versioned = [p for p in both if old[p] and new[p]]
    if both and len(versioned) < len(both):
        side = "the evidence" if not all(old[p] for p in both) else "the current side"
        notes.append(f"{name}: package versions are not compared where {side} has none")
    found += [
        Change("packages", "changed", p, variant=name, old=old[p], new=new[p])
        for p in versioned
        if old[p] != new[p]
    ]
    return found


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
    new_digest = tree_rows_digest((name, row) for row in rows)
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
    notes: list[str],
) -> list[Change]:
    record = index["variants"].get(name) or {}
    policy = record.get("policy")
    member = policy.get("member") if isinstance(policy, dict) else None
    old = _registers(_json(files.get(str(member))) if member else None)
    current = None if build is None else Path(build) / name / "policy.json"
    new = _registers(_json(current.read_bytes()) if current and current.is_file() else None)
    if old is None or new is None:
        if old is not None or new is not None or build is not None:
            side = "the evidence" if old is None else f"{current}"
            notes.append(f"{name}: no measurements policy in {side}: registers are not compared")
        return []
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


def _registers(payload: Any) -> dict[str, str] | None:
    if not isinstance(payload, dict):
        return None
    try:
        checked = read_policy(payload)
    except TdxError:
        return None
    return {str(k): str(v) for k, v in dict(checked.get("registers") or {}).items()}  # type: ignore[call-overload]


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
    subject = "Package" if name == "packages" else "Register"
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
    "SECTIONS",
    "Change",
    "ChangeReport",
    "changes",
]
