"""Lockfile parser and serializer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tundravm.errors import LockfileError
from tundravm.lockfile.model import LockedFetch, Lockfile


def serialize_lockfile(lockfile: Lockfile) -> str:
    payload = {
        "version": lockfile.version,
        "recipe_digest": lockfile.recipe_digest,
        "recipe": lockfile.recipe,
        "dependencies": lockfile.dependencies,
        "fetches": [
            {"source": item.source, "kind": item.kind, "digest": item.digest}
            for item in lockfile.fetches
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def parse_lockfile(raw: str) -> Lockfile:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LockfileError("Invalid lockfile JSON.", hint=str(exc)) from exc

    if not isinstance(payload, dict):
        raise LockfileError("Invalid lockfile payload type.")

    version = _required_int(payload, "version")
    recipe_digest = _required_str(payload, "recipe_digest")
    recipe = _required_dict(payload, "recipe")
    dependencies = _required_dependencies(payload, "dependencies")
    fetches_raw = payload.get("fetches", [])
    if not isinstance(fetches_raw, list):
        raise LockfileError("Invalid lockfile `fetches` value.")
    fetches = [_parse_locked_fetch(item) for item in fetches_raw]
    return Lockfile(
        version=version,
        recipe_digest=recipe_digest,
        recipe=recipe,
        dependencies=dependencies,
        fetches=fetches,
    )


def read_lockfile(path: str | Path) -> Lockfile:
    lock_path = Path(path)
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise LockfileError(
            "Lockfile does not exist.",
            hint="Run img.lock() before using frozen mode.",
            context={"path": str(lock_path)},
        ) from exc
    return parse_lockfile(raw)


def write_lockfile(lockfile: Lockfile, path: str | Path) -> Path:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(serialize_lockfile(lockfile), encoding="utf-8")
    return lock_path


def _parse_locked_fetch(item: Any) -> LockedFetch:
    if not isinstance(item, dict):
        raise LockfileError("Invalid fetch entry in lockfile.")
    return LockedFetch(
        source=_required_str(item, "source"),
        kind=_required_str(item, "kind"),
        digest=_required_str(item, "digest"),
    )


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise LockfileError(f"Invalid lockfile `{key}` value.")
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise LockfileError(f"Invalid lockfile `{key}` value.")
    return value


def _required_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise LockfileError(f"Invalid lockfile `{key}` value.")
    return value


def _required_dependencies(payload: dict[str, Any], key: str) -> dict[str, list[str]]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise LockfileError(f"Invalid lockfile `{key}` value.")
    parsed: dict[str, list[str]] = {}
    for profile, packages in value.items():
        if not isinstance(profile, str):
            raise LockfileError("Invalid lockfile dependency profile key.")
        if not isinstance(packages, list) or not all(isinstance(item, str) for item in packages):
            raise LockfileError("Invalid lockfile dependency package list.")
        parsed[profile] = list(packages)
    return parsed
