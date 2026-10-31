"""Measurement scheme dispatch and model exports."""

from __future__ import annotations

from pathlib import Path

from tundravm.errors import MeasurementError
from tundravm.measure.model import (
    MEASURE_TOOL,
    MEASURE_VERSION,
    MeasurementBackend,
    Measurements,
    MeasurementSource,
)
from tundravm.models import ProfileBuildResult

from . import schemes


def derive_measurements(
    *,
    backend: MeasurementBackend,
    profile: str,
    profile_result: ProfileBuildResult,
) -> Measurements:
    """Measure *profile_result*'s artifact for scheme *backend*."""
    if not profile_result.artifacts:
        raise MeasurementError(
            "No artifacts are available for measurement derivation.",
            hint="Bake the variant before measuring it.",
            context={"profile": profile, "backend": backend},
        )
    _, artifact = sorted(profile_result.artifacts.items())[0]
    payload = artifact.boot_payload
    return schemes.derive(
        backend,
        artifact=Path(artifact.path),
        uki=None if payload is None else Path(payload),
    )


__all__ = [
    "MEASURE_TOOL",
    "MEASURE_VERSION",
    "MeasurementBackend",
    "MeasurementSource",
    "Measurements",
    "derive_measurements",
]
