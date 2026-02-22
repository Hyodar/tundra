"""GCP PCR-like measurement derivation."""

from __future__ import annotations

import hashlib


def derive(profile: str, artifact_digests: dict[str, str]) -> dict[str, str]:
    digest_payload = "|".join(f"{key}:{value}" for key, value in sorted(artifact_digests.items()))
    return {
        "PCR0": _sha256(f"gcp:{digest_payload}"),
        "PCR4": _sha256(f"profile:{profile}"),
        "PCR8": _sha256(f"targets:{','.join(sorted(artifact_digests))}"),
    }


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
