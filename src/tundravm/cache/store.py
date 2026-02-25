"""Content-addressed cache store with manifest verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tundravm.cache.keys import BuildCacheInput, _to_payload, cache_key
from tundravm.errors import ReproducibilityError


class BuildCacheStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, *, key: str, expected_inputs: BuildCacheInput) -> bytes | None:
        entry = self.root / key
        artifact_path = entry / "artifact.bin"
        manifest_path = entry / "manifest.json"
        if not artifact_path.exists() or not manifest_path.exists():
            return None

        manifest = self._read_manifest(manifest_path)
        expected_payload = _to_payload(expected_inputs)
        if manifest.get("inputs") != expected_payload:
            raise ReproducibilityError(
                "Cache manifest inputs do not match expected build inputs.",
                hint="Invalidate the cache entry and rebuild.",
                context={"operation": "cache_load", "key": key},
            )
        if manifest.get("key") != key:
            raise ReproducibilityError(
                "Cache manifest key mismatch.",
                hint="Invalidate the cache entry and rebuild.",
                context={"operation": "cache_load", "key": key},
            )

        payload = artifact_path.read_bytes()
        actual_digest = hashlib.sha256(payload).hexdigest()
        if manifest.get("artifact_sha256") != actual_digest:
            raise ReproducibilityError(
                "Cache artifact digest mismatch.",
                hint="Invalidate the cache entry and rebuild.",
                context={"operation": "cache_load", "key": key},
            )
        return payload

    def save(self, *, inputs: BuildCacheInput, artifact: bytes) -> str:
        key = cache_key(inputs)
        entry = self.root / key
        entry.mkdir(parents=True, exist_ok=True)

        artifact_path = entry / "artifact.bin"
        manifest_path = entry / "manifest.json"
        artifact_path.write_bytes(artifact)
        manifest = {
            "key": key,
            "inputs": _to_payload(inputs),
            "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return key

    def _read_manifest(self, path: Path) -> dict[str, object]:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ReproducibilityError(
                "Cache manifest is not valid JSON.",
                hint="Invalidate the cache entry and rebuild.",
                context={"operation": "cache_load", "path": str(path)},
            ) from exc
        if not isinstance(parsed, dict):
            raise ReproducibilityError(
                "Cache manifest has invalid structure.",
                hint="Invalidate the cache entry and rebuild.",
                context={"operation": "cache_load", "path": str(path)},
            )
        return parsed
