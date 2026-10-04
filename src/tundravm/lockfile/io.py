"""Lockfile parser and serializer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tundravm.errors import LockfileError
from tundravm.lockfile.model import LOCKFILE_VERSION, LockedFetch, Lockfile
from tundravm.lockfile.resolve import VARIANTS_SECTION

_REGENERATE = "The lockfile is generated: run `tundravm lock RECIPE` to rewrite it."


def serialize_lockfile(lockfile: Lockfile) -> str:
    payload = {
        "version": lockfile.version,
        "recipe_digest": lockfile.recipe_digest,
        "recipe": lockfile.recipe,
        "dependencies": lockfile.dependencies,
        "fetches": [_fetch_payload(item) for item in lockfile.fetches],
        "sections": lockfile.sections,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def parse_lockfile(raw: str) -> Lockfile:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LockfileError("Invalid lockfile JSON.", hint=str(exc)) from exc

    if not isinstance(payload, dict):
        raise LockfileError("Invalid lockfile payload type.", hint=_REGENERATE)

    version = _required_int(payload, "version")
    recipe_digest = _required_str(payload, "recipe_digest")
    recipe = _required_dict(payload, "recipe")
    dependencies = _required_dependencies(payload, "dependencies")
    fetches_raw = payload.get("fetches", [])
    if not isinstance(fetches_raw, list):
        raise LockfileError("Invalid lockfile `fetches` value.", hint=_REGENERATE)
    fetches = [_parse_locked_fetch(item) for item in fetches_raw]
    sections = _optional_sections(payload, "sections")
    if version == _PROFILE_SECTIONS_VERSION:
        version = LOCKFILE_VERSION
        sections = {_variant_section(name): digest for name, digest in sections.items()}
    return Lockfile(
        version=version,
        recipe_digest=recipe_digest,
        recipe=recipe,
        dependencies=dependencies,
        fetches=fetches,
        sections=sections,
    )


_PROFILE_SECTIONS_VERSION = 2
"""The lockfile version whose per-variant sections are named ``profiles.<name>.<key>``."""


def _variant_section(name: str) -> str:
    old = "profiles."
    return VARIANTS_SECTION + name[len(old) - 1 :] if name.startswith(old) else name


def read_lockfile(path: str | Path) -> Lockfile:
    lock_path = Path(path)
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise LockfileError(
            "Lockfile does not exist.",
            hint="Run `tundravm lock RECIPE` (or tundravm.lock(recipe)) to create it.",
            context={"path": str(lock_path)},
        ) from exc
    return parse_lockfile(raw)


def _fetch_payload(item: LockedFetch) -> dict[str, str]:
    payload = {"source": item.source, "kind": item.kind, "digest": item.digest}
    if item.name is not None:
        payload["name"] = item.name
    if item.ref is not None:
        payload["ref"] = item.ref
    return payload


def _parse_locked_fetch(item: Any) -> LockedFetch:
    if not isinstance(item, dict):
        raise LockfileError("Invalid fetch entry in lockfile.", hint=_REGENERATE)
    return LockedFetch(
        source=_required_str(item, "source"),
        kind=_required_str(item, "kind"),
        digest=_required_str(item, "digest"),
        name=_optional_str(item, "name"),
        ref=_optional_str(item, "ref"),
    )


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    if payload.get(key) is None:
        return None
    return _required_str(payload, key)


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise LockfileError(f"Invalid lockfile `{key}` value.", hint=_REGENERATE)
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise LockfileError(f"Invalid lockfile `{key}` value.", hint=_REGENERATE)
    return value


def _required_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise LockfileError(f"Invalid lockfile `{key}` value.", hint=_REGENERATE)
    return value


def _required_dependencies(payload: dict[str, Any], key: str) -> dict[str, list[str]]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise LockfileError(f"Invalid lockfile `{key}` value.", hint=_REGENERATE)
    parsed: dict[str, list[str]] = {}
    for profile, packages in value.items():
        if not isinstance(profile, str):
            raise LockfileError("Invalid lockfile dependency variant key.", hint=_REGENERATE)
        if not isinstance(packages, list) or not all(isinstance(item, str) for item in packages):
            raise LockfileError("Invalid lockfile dependency package list.", hint=_REGENERATE)
        parsed[profile] = list(packages)
    return parsed


def _optional_sections(payload: dict[str, Any], key: str) -> dict[str, str]:
    value = payload.get(key, {})
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(digest, str) for name, digest in value.items()
    ):
        raise LockfileError(f"Invalid lockfile `{key}` value.", hint=_REGENERATE)
    return dict(value)
