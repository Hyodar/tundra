"""Measurement backend dispatch and model exports."""

from __future__ import annotations

import hashlib
from pathlib import Path

from tundravm.errors import MeasurementError
from tundravm.measure.model import (
    MeasurementBackend,
    Measurements,
    MeasurementSource,
    PlaceholderMeasurementWarning,
)
from tundravm.models import ProfileBuildResult

from . import azure, gcp, rtmr
from .rtmr import ToolLocator, ToolRunner


def derive_measurements(
    *,
    backend: MeasurementBackend,
    profile: str,
    profile_result: ProfileBuildResult,
    allow_placeholder: bool = False,
    tool_locator: ToolLocator | None = None,
    runner: ToolRunner | None = None,
) -> Measurements:
    """Measure *profile_result*'s artifacts; placeholder values need ``allow_placeholder``."""
    artifact_paths, digests_by_target, digests_by_path = _artifact_data(profile_result)
    if not digests_by_target:
        raise MeasurementError(
            "No artifacts are available for measurement derivation.",
            hint="Bake the variant before measuring it.",
            context={"profile": profile, "backend": backend},
        )
    if backend == "rtmr":
        return rtmr.derive(
            profile,
            artifact_digests=digests_by_path,
            artifact_paths=artifact_paths,
            allow_placeholder=allow_placeholder,
            tool_locator=tool_locator,
            runner=runner,
        )
    if backend == "azure":
        return azure.derive(profile, digests_by_target, allow_placeholder=allow_placeholder)
    if backend == "gcp":
        return gcp.derive(profile, digests_by_target, allow_placeholder=allow_placeholder)
    raise MeasurementError("Unsupported measurement backend.", context={"backend": backend})


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
    "MeasurementBackend",
    "MeasurementSource",
    "Measurements",
    "PlaceholderMeasurementWarning",
    "ToolLocator",
    "ToolRunner",
    "derive_measurements",
]
