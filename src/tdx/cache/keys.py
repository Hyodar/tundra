"""Cache key derivation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class BuildCacheInput:
    source_hash: str
    source_tree: str
    toolchain: str
    flags: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    target: str = "qemu"


def cache_key(inputs: BuildCacheInput) -> str:
    canonical = json.dumps(_to_payload(inputs), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_payload(inputs: BuildCacheInput) -> dict[str, Any]:
    return {
        "source_hash": inputs.source_hash,
        "source_tree": inputs.source_tree,
        "toolchain": inputs.toolchain,
        "flags": list(inputs.flags),
        "dependencies": list(inputs.dependencies),
        "env": dict(sorted(inputs.env.items())),
        "target": inputs.target,
    }
