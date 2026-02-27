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
    artifact_paths, digests_by_target, digests_by_path = _artifact_data(profile_result)
    if not digests_by_target:
        raise MeasurementError(
            "No artifacts are available for measurement derivation.",
            hint="Bake profile artifacts before requesting measurements.",
            context={"profile": profile, "backend": backend},
        )
    if backend == "rtmr":
        values = rtmr.derive(
            profile,
            artifact_digests=digests_by_path,
            artifact_paths=artifact_paths,
        )
    elif backend == "azure":
        values = azure.derive(profile, digests_by_target)
    elif backend == "gcp":
        values = gcp.derive(profile, digests_by_target)
    else:
        raise MeasurementError("Unsupported measurement backend.", context={"backend": backend})
    return Measurements(backend=backend, values=values)


def _artifact_data(
    profile_result: ProfileBuildResult,
) -> tuple[tuple[Path, ...], dict[str, str], dict[str, str]]:
    artifact_paths: list[Path] = []
    digests_by_target: dict[str, str] = {}
    digests_by_path: dict[str, str] = {}
    for target, artifact in sorted(profile_result.artifacts.items()):
        path = Path(artifact.path)
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        artifact_paths.append(path)
        digests_by_target[target] = digest
        digests_by_path[str(path)] = digest
    return tuple(artifact_paths), digests_by_target, digests_by_path


__all__ = [
    "MeasurementMismatch",
    "Measurements",
    "VerificationResult",
    "derive_measurements",
]
