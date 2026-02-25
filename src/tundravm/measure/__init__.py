"""Measurement backend dispatch and model exports."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from tundravm.errors import MeasurementError
from tundravm.measure.model import MeasurementMismatch, Measurements, VerificationResult
from tundravm.models import ProfileBuildResult

from . import azure, gcp, rtmr


def derive_measurements(
    *,
    backend: Literal["rtmr", "azure", "gcp"],
    profile: str,
    profile_result: ProfileBuildResult,
) -> Measurements:
    artifact_digests = _artifact_digests(profile_result)
    if not artifact_digests:
        raise MeasurementError(
            "No artifacts are available for measurement derivation.",
            hint="Bake profile artifacts before requesting measurements.",
            context={"profile": profile, "backend": backend},
        )
    if backend == "rtmr":
        values = rtmr.derive(profile, artifact_digests)
    elif backend == "azure":
        values = azure.derive(profile, artifact_digests)
    elif backend == "gcp":
        values = gcp.derive(profile, artifact_digests)
    else:
        raise MeasurementError("Unsupported measurement backend.", context={"backend": backend})
    return Measurements(backend=backend, values=values)


def _artifact_digests(profile_result: ProfileBuildResult) -> dict[str, str]:
    digests: dict[str, str] = {}
    for target, artifact in sorted(profile_result.artifacts.items()):
        payload = Path(artifact.path).read_bytes()
        digests[target] = hashlib.sha256(payload).hexdigest()
    return digests


__all__ = [
    "MeasurementMismatch",
    "Measurements",
    "VerificationResult",
    "derive_measurements",
]
