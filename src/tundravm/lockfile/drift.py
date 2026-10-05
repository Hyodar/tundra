"""Explain how a recipe has drifted from its lockfile, section by section."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tundravm.formats import annotation_path, md_cell, md_table, workflow_command
from tundravm.lockfile.model import LOCKFILE_VERSION, Lockfile
from tundravm.lockfile.resolve import (
    VARIANTS_KEY,
    VARIANTS_SECTION,
    recipe_digest,
    section_values,
    value_digest,
)

DETAIL_LIMIT = 5
"""Maximum item entries shown per section detail before eliding the rest."""

_IDENTITY_KEYS = ("path", "name")
_SHORT_LIMIT = 40


@dataclass(frozen=True, slots=True)
class LockDrift:
    """Section-level difference between a lockfile and the current recipe.

    ``changed`` lists dotted sections on both sides whose digest differs,
    ``added`` sections only the recipe has (every section when the lockfile
    predates section digests) and ``removed`` sections only the lockfile has.
    ``details`` holds a one-line item summary for some changed sections, such as
    ``+htop -jq`` for packages or ``~/etc/motd`` for files. ``digest_matches``
    reports whether the whole-recipe digest, which frozen bakes enforce, still
    matches; it stays true when only some of the lock's variants were compared.
    ``lock_version`` is the version of a lockfile older than
    :data:`~tundravm.lockfile.LOCKFILE_VERSION` (``None``: current), which drifts
    until it is locked again.
    """

    changed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    details: dict[str, str] = field(default_factory=dict)
    digest_matches: bool = True
    lock_version: int | None = None

    @property
    def is_clean(self) -> bool:
        """True when the lockfile is current and matches the recipe and every section digest."""
        return (
            self.lock_version is None
            and self.digest_matches
            and not (self.changed or self.added or self.removed)
        )

    @property
    def sections(self) -> tuple[str, ...]:
        """Every drifted section name, sorted."""
        return tuple(sorted({*self.changed, *self.added, *self.removed}))

    def render(self) -> str:
        """One line per drifted section (``~``, ``+``, ``-``), or ``lock is up to date``."""
        if self.is_clean:
            return "lock is up to date"
        lines: list[str] = []
        for marker, name, detail in self._entries():
            line = f"{marker} {name}"
            lines.append(f"{line}: {detail}" if detail else line)
        return "\n".join(lines)

    def github(self, lock_path: str | Path | None = None) -> str:
        """One ``::error`` workflow command per drifted section, or ``lock is up to date``."""
        if self.is_clean:
            return "lock is up to date"
        path = None if lock_path is None else annotation_path(lock_path)
        lines: list[str] = []
        for marker, name, detail in self._entries():
            message = f"{name} {_DRIFT_WORDS[marker]}"
            if detail:
                message += f": {detail}"
            message += ". Run `tundravm lock` and commit the lockfile."
            lines.append(workflow_command("error", message, file=path, title="lock drift"))
        return "\n".join(lines)

    def markdown(self) -> str:
        """A Markdown table of drifted sections, or a bold up-to-date line."""
        if self.is_clean:
            return "**Lock is up to date.**"
        rows = [
            (md_cell(_DRIFT_WORDS[marker]), md_cell(name, code=True), md_cell(detail, code=True))
            for marker, name, detail in self._entries()
        ]
        return md_table(("Change", "Section", "Detail"), rows)

    def _entries(self) -> list[tuple[str, str, str | None]]:
        """``(marker, section, detail)`` per drifted section, sorted by section."""
        markers = {name: "~" for name in self.changed}
        markers.update({name: "+" for name in self.added})
        markers.update({name: "-" for name in self.removed})
        entries = [(markers[name], name, self.details.get(name)) for name in sorted(markers)]
        if self.lock_version is not None:
            entries.insert(
                0,
                (
                    "~",
                    "version",
                    f"{self.lock_version} -> {LOCKFILE_VERSION}: lock again to record the "
                    "distribution, compiler and kernel sections",
                ),
            )
        if not entries and not self.digest_matches:
            entries.append(
                ("~", "recipe_digest", "every section matches; the lockfile digest was edited")
            )
        return entries


_DRIFT_WORDS = {
    "~": "changed",
    "+": "added (not in the lockfile)",
    "-": "removed (only in the lockfile)",
}


def lock_variants(lock: Lockfile) -> tuple[str, ...]:
    """The variants *lock* was written for, sorted."""
    return _variants(lock.recipe)


def _variants(payload: Mapping[str, object]) -> tuple[str, ...]:
    entries = payload.get(VARIANTS_KEY)
    return tuple(sorted(str(name) for name in entries)) if isinstance(entries, Mapping) else ()


def unselected_variants(lock: Lockfile, payload: Mapping[str, object]) -> tuple[str, ...]:
    """The variants *lock* holds that the recipe *payload* does not select."""
    selected = set(_variants(payload))
    return tuple(name for name in lock_variants(lock) if name not in selected)


def unselected_sources(lock: Lockfile, payload: Mapping[str, object]) -> frozenset[str]:
    """Source keys the lock may record for variants the recipe *payload* does not select.

    Their source builds (by name, and as ``<variant>/<name>`` when pinned per
    variant) and kernel sources: ``kernel-<variant>``, and the shared ``kernel``,
    which an unselected variant may build too.
    """
    entries = lock.recipe.get(VARIANTS_KEY)
    if not isinstance(entries, Mapping):
        return frozenset()
    names: set[str] = set()
    for variant in unselected_variants(lock, payload):
        names.update(("kernel", f"kernel-{variant}"))
        entry = entries.get(variant)
        builds = entry.get("source_builds") if isinstance(entry, Mapping) else None
        if isinstance(builds, Mapping):
            names.update(str(name) for name in builds)
            names.update(f"{variant}/{name}" for name in builds)
    return frozenset(names)


def _section_variant(name: str, variants: Sequence[str]) -> str | None:
    """The variant a ``variants.<name>.<key>`` section belongs to (longest name wins)."""
    found = [v for v in variants if name.startswith(f"{VARIANTS_SECTION}.{v}.")]
    return max(found, key=len, default=None)


def compare_lock(
    lock: Lockfile, payload: Mapping[str, object], *, partial: bool = False
) -> LockDrift:
    """Compare *lock* against the current recipe *payload* section by section.

    Item-level detail comes from the recipe payload embedded in the lockfile and
    is only used when that embedded sub-payload still hashes to the section
    digest the lockfile recorded.

    *partial* says *payload* holds a selection of the recipe's variants: the
    lock's sections of variants outside it are not compared, and the
    whole-recipe digest only when the selection is every variant the lock holds.
    A lock written for more variants thus covers a bake or check of fewer.
    """
    current = section_values(payload)
    locked = lock.sections
    others = unselected_variants(lock, payload) if partial else ()
    if others:
        known = (*_variants(payload), *lock_variants(lock))
        locked = {
            name: digest
            for name, digest in locked.items()
            if _section_variant(name, known) not in others
        }
    changed = tuple(
        name
        for name, value in current.items()
        if name in locked and locked[name] != value_digest(value)
    )
    added = tuple(name for name in current if name not in locked)
    removed = tuple(sorted(name for name in locked if name not in current))
    previous = section_values(lock.recipe)
    details: dict[str, str] = {}
    for name in changed:
        if name not in previous or value_digest(previous[name]) != locked[name]:
            continue
        detail = describe_change(previous[name], current[name])
        if detail:
            details[name] = detail
    return LockDrift(
        changed=changed,
        added=added,
        removed=removed,
        details=details,
        digest_matches=bool(others) or lock.recipe_digest == recipe_digest(payload),
        lock_version=lock.version if lock.version < LOCKFILE_VERSION else None,
    )


def describe_change(old: object, new: object, *, limit: int = DETAIL_LIMIT) -> str | None:
    """Diff two JSON-ish values one level deep and format the result on one line.

    Dicts diff by key; lists of strings by item; lists of objects by their
    ``path`` or ``name`` field (``+`` added, ``-`` removed, ``~`` modified);
    scalars render as ``old -> new``. At most *limit* entries are shown, then a
    ``… +N more`` tail. Returns None when nothing item-level can be said.
    """
    if _is_scalar(old) and _is_scalar(new):
        return f"{_short(old)} -> {_short(new)}"
    if not _is_collection(old) or not _is_collection(new):
        return None
    old_items = _keyed(old)
    new_items = _keyed(new)
    if old_items is None or new_items is None:
        old_items = _by_content(old)
        new_items = _by_content(new)
    entries = [f"+{_trim(key)}" for key in sorted(new_items.keys() - old_items.keys())]
    entries += [f"-{_trim(key)}" for key in sorted(old_items.keys() - new_items.keys())]
    entries += [
        f"~{_trim(key)}"
        for key in sorted(old_items.keys() & new_items.keys())
        if value_digest(old_items[key]) != value_digest(new_items[key])
    ]
    if not entries:
        return None
    shown = entries[:limit]
    if len(entries) > limit:
        shown.append(f"… +{len(entries) - limit} more")
    return " ".join(shown)


def _is_collection(value: object) -> bool:
    return isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, str)
    )


def _keyed(value: object) -> dict[str, object] | None:
    """Index *value* by item identity; None when two items share an identity."""
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    keyed: dict[str, object] = {}
    for item in _items(value):
        key = _identity(item)
        if key in keyed:
            return None
        keyed[key] = item
    return keyed


def _identity(item: object) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        for field_name in _IDENTITY_KEYS:
            identity = item.get(field_name)
            if isinstance(identity, str):
                return identity
    return _canonical(item)


def _by_content(value: object) -> dict[str, object]:
    return {_canonical(item): item for item in _items(value)}


def _items(value: object) -> list[object]:
    if isinstance(value, Mapping):
        return [{str(key): item} for key, item in value.items()]
    return list(value) if isinstance(value, Sequence) else []


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _short(value: object) -> str:
    return _trim(_canonical(value))


def _trim(text: str) -> str:
    return text if len(text) <= _SHORT_LIMIT else text[: _SHORT_LIMIT - 1] + "…"
