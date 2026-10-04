"""Lockfile resolution helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from tundravm.lockfile.model import LOCKFILE_VERSION, LockedFetch, Lockfile

VARIANTS_KEY = "profiles"
"""The payload key holding one entry per variant (kept so recipe digests do not move)."""
VARIANTS_SECTION = "variants"
"""The section prefix of per-variant sections: ``variants.<name>.<key>``."""
NESTED_SECTIONS = {VARIANTS_KEY: VARIANTS_SECTION}
"""Top-level payload keys whose ``<name>.<section>`` children are digested individually,
mapped to the prefix their section names use."""


def value_digest(value: object) -> str:
    """Return the sha256 of *value* in the canonical JSON encoding used by lockfiles."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def recipe_digest(recipe: Mapping[str, Any]) -> str:
    """Return the digest of the whole recipe payload (the value frozen bakes enforce)."""
    return value_digest(dict(recipe))


def section_values(payload: Mapping[str, object]) -> dict[str, object]:
    """Split *payload* into dotted sections.

    Every top-level key is one section, except the per-variant ``profiles``
    payload: each variant's own keys become ``variants.<name>.<key>`` sections.
    """
    sections: dict[str, object] = {}
    for key, value in payload.items():
        prefix = NESTED_SECTIONS.get(str(key))
        if prefix is not None and isinstance(value, Mapping):
            for name, entry in value.items():
                if isinstance(entry, Mapping):
                    for section, item in entry.items():
                        sections[f"{prefix}.{name}.{section}"] = item
                else:
                    sections[f"{prefix}.{name}"] = entry
            continue
        sections[str(key)] = value
    return dict(sorted(sections.items()))


def section_digests(payload: Mapping[str, object]) -> dict[str, str]:
    """Map each dotted section of *payload* (see :func:`section_values`) to its sha256."""
    return {name: value_digest(value) for name, value in section_values(payload).items()}


def build_lockfile(
    *,
    recipe: dict[str, Any],
    fetches: list[LockedFetch] | None = None,
) -> Lockfile:
    dependencies: dict[str, list[str]] = {}
    profiles = recipe.get("profiles", {})
    if isinstance(profiles, dict):
        for profile_name, profile_data in profiles.items():
            if not isinstance(profile_name, str) or not isinstance(profile_data, dict):
                continue
            packages = profile_data.get("packages", [])
            if isinstance(packages, list) and all(isinstance(item, str) for item in packages):
                dependencies[profile_name] = list(packages)

    return Lockfile(
        version=LOCKFILE_VERSION,
        recipe_digest=recipe_digest(recipe),
        recipe=recipe,
        dependencies=dependencies,
        fetches=list(fetches or []),
        sections=section_digests(recipe),
    )
