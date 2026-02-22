"""Integrity-enforced HTTP/file fetch implementation."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.request import urlopen

from tdx.errors import ReproducibilityError, ValidationError
from tdx.policy import Policy, ensure_network_allowed


def fetch(
    url: str,
    *,
    sha256: str,
    cache_dir: str | Path,
    policy: Policy | None = None,
) -> Path:
    """Fetch content and return a content-addressed cached path."""
    if policy is not None:
        ensure_network_allowed(policy=policy, operation="fetch")
    if not sha256:
        if policy is not None and not policy.require_integrity:
            return _fetch_without_integrity(url=url, cache_dir=cache_dir)
        raise ValidationError("fetch() requires a sha256 value.")
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    artifact_path = cache_path / sha256

    if artifact_path.exists():
        _assert_hash_matches(artifact_path, expected_sha256=sha256)
        return artifact_path

    with urlopen(url) as response:  # noqa: S310 - integrity check is mandatory below
        payload = response.read()

    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != sha256:
        raise ReproducibilityError(
            "Fetched content hash mismatch.",
            hint="Update the expected hash or source URL to a trusted immutable artifact.",
            context={"operation": "fetch", "url": url, "expected": sha256, "actual": actual_sha256},
        )

    temp_path = artifact_path.with_suffix(".tmp")
    temp_path.write_bytes(payload)
    os.replace(temp_path, artifact_path)
    return artifact_path


def _fetch_without_integrity(*, url: str, cache_dir: str | Path) -> Path:
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    with urlopen(url) as response:  # noqa: S310 - policy explicitly allows non-integrity mode
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    artifact_path = cache_path / digest
    if not artifact_path.exists():
        artifact_path.write_bytes(payload)
    return artifact_path


def _assert_hash_matches(path: Path, *, expected_sha256: str) -> None:
    actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ReproducibilityError(
            "Cached artifact hash mismatch.",
            hint="Clear cache and refetch with trusted inputs.",
            context={
                "operation": "fetch",
                "path": str(path),
                "expected": expected_sha256,
                "actual": actual_sha256,
            },
        )
