"""Lockfile resolution helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from tundravm.lockfile.model import LockedFetch, Lockfile


def recipe_digest(recipe: dict[str, Any]) -> str:
    canonical = json.dumps(recipe, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        version=1,
        recipe_digest=recipe_digest(recipe),
        recipe=recipe,
        dependencies=dependencies,
        fetches=list(fetches or []),
    )
