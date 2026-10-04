"""Explain how a recipe has drifted from its lockfile, section by section."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from tundravm.lockfile.model import Lockfile
from tundravm.lockfile.resolve import recipe_digest, section_values, value_digest

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
    matches.
    """

    changed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    details: dict[str, str] = field(default_factory=dict)
    digest_matches: bool = True

    @property
    def is_clean(self) -> bool:
        """True when the lockfile matches the recipe digest and every section digest."""
        return self.digest_matches and not (self.changed or self.added or self.removed)

    @property
    def sections(self) -> tuple[str, ...]:
        """Every drifted section name, sorted."""
        return tuple(sorted({*self.changed, *self.added, *self.removed}))

    def render(self) -> str:
        """One line per drifted section (``~``, ``+``, ``-``), or ``lock is up to date``."""
        if self.is_clean:
            return "lock is up to date"
        markers = {name: "~" for name in self.changed}
        markers.update({name: "+" for name in self.added})
        markers.update({name: "-" for name in self.removed})
        lines: list[str] = []
        for name in sorted(markers):
            line = f"{markers[name]} {name}"
            detail = self.details.get(name)
            lines.append(f"{line}: {detail}" if detail else line)
        if not lines:
            lines.append("~ recipe_digest (every section matches; the lockfile digest was edited)")
        return "\n".join(lines)


def compare_lock(lock: Lockfile, payload: Mapping[str, object]) -> LockDrift:
    """Compare *lock* against the current recipe *payload* section by section.

    Item-level detail comes from the recipe payload embedded in the lockfile and
    is only used when that embedded sub-payload still hashes to the section
    digest the lockfile recorded.
    """
    current = section_values(payload)
    locked = lock.sections
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
        digest_matches=lock.recipe_digest == recipe_digest(payload),
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
