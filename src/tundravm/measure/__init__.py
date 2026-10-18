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
    PlaceholderMeasurementWarning,
)
from tundravm.models import ProfileBuildResult

from . import schemes


def derive_measurements(
    *,
    backend: MeasurementBackend,
    profile: str,
    profile_result: ProfileBuildResult,
    allow_placeholder: bool = False,
) -> Measurements:
    """Measure *profile_result*'s artifact for scheme *backend*; placeholder values (for an
    artifact that is no UKI or disk, such as a simulated one) need ``allow_placeholder``."""
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
        allow_placeholder=allow_placeholder,
    )


__all__ = [
    "MEASURE_TOOL",
    "MEASURE_VERSION",
    "MeasurementBackend",
    "MeasurementSource",
    "Measurements",
    "PlaceholderMeasurementWarning",
    "derive_measurements",
]
