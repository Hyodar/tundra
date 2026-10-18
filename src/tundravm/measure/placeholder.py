"""Opt-in placeholder measurements: digest-derived values that no TEE reproduces."""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from pathlib import Path

from tundravm.measure.model import MeasurementBackend, Measurements, PlaceholderMeasurementWarning

ALLOW_PLACEHOLDER_HINT = (
    "Pass allow_placeholder=True (CLI: --allow-placeholder) to get placeholder values "
    "for tests; never put them in an attestation policy."
)

_PACKAGE_DIR = str(Path(__file__).resolve().parent.parent)


def placeholder_measurements(
    backend: MeasurementBackend,
    measurements: Measurements,
    *,
    context: Mapping[str, str] | None = None,
) -> Measurements:
    """Warn that *measurements* are placeholders (``allow_placeholder`` was given) and
    return them."""
    reason = (context or {}).get("reason")
    warnings.warn(
        PlaceholderMeasurementWarning(
            f"{backend} measurements are placeholders derived from artifact digests, "
            "not real measurements; do not use them in an attestation policy"
            + (f" ({reason})." if reason else ".")
        ),
        skip_file_prefixes=(_PACKAGE_DIR,),
    )
    return measurements
