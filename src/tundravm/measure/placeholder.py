"""Opt-in placeholder measurements: digest-derived values that no TEE reproduces."""

from __future__ import annotations

import hashlib
import warnings
from collections.abc import Mapping
from pathlib import Path

from tundravm.errors import MeasurementError
from tundravm.measure.model import MeasurementBackend, Measurements, PlaceholderMeasurementWarning

ALLOW_PLACEHOLDER_HINT = (
    "Pass allow_placeholder=True (CLI: --allow-placeholder) to get placeholder values "
    "for tests; never put them in an attestation policy."
)

_PACKAGE_DIR = str(Path(__file__).resolve().parent.parent)


def placeholder_measurements(
    backend: MeasurementBackend,
    values: dict[str, str],
    *,
    allow_placeholder: bool,
    message: str,
    hint: str,
    context: Mapping[str, str] | None = None,
) -> Measurements:
    """Return ``source="placeholder"`` values with a warning, or refuse with ``E_MEASUREMENT``."""
    if not allow_placeholder:
        raise MeasurementError(
            message,
            hint=f"{hint} {ALLOW_PLACEHOLDER_HINT}",
            context={"backend": backend, **(context or {})},
        )
    warnings.warn(
        PlaceholderMeasurementWarning(
            f"{backend} measurements are placeholders derived from artifact digests, "
            "not real measurements; do not use them in an attestation policy."
        ),
        skip_file_prefixes=(_PACKAGE_DIR,),
    )
    return Measurements(backend=backend, values=values, source="placeholder")


def digest_values(
    registers: tuple[str, str, str],
    *,
    salt: str,
    profile: str,
    artifact_digests: Mapping[str, str],
) -> dict[str, str]:
    """Deterministic SHA-256 stand-ins for three registers (artifacts, profile, targets)."""
    digest_payload = "|".join(f"{key}:{value}" for key, value in sorted(artifact_digests.items()))
    first, second, third = registers
    return {
        first: _sha256(f"{salt}{digest_payload}"),
        second: _sha256(f"profile:{profile}"),
        third: _sha256(f"targets:{','.join(sorted(artifact_digests))}"),
    }


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
