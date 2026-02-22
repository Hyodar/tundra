"""RTMR measurement derivation."""

from __future__ import annotations

import hashlib


def derive(profile: str, artifact_digests: dict[str, str]) -> dict[str, str]:
    digest_payload = "|".join(f"{key}:{value}" for key, value in sorted(artifact_digests.items()))
    return {
        "RTMR0": _sha256(digest_payload),
        "RTMR1": _sha256(f"profile:{profile}"),
        "RTMR2": _sha256(f"targets:{','.join(sorted(artifact_digests))}"),
    }


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
