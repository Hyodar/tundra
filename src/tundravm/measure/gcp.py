"""GCP PCR-like measurement placeholder.

No tool integration exists: the values are SHA-256 digests of the artifact
digests, not PCRs any GCP confidential VM reports.
"""

from __future__ import annotations

from tundravm.measure.model import Measurements
from tundravm.measure.placeholder import digest_values, placeholder_measurements

HINT = (
    "GCP measurement derivation is a placeholder; use the cloud's attestation report "
    "to obtain real values."
)


def derive(
    profile: str,
    artifact_digests: dict[str, str],
    *,
    allow_placeholder: bool = False,
) -> Measurements:
    values = digest_values(
        ("PCR0", "PCR4", "PCR8"),
        salt="gcp:",
        profile=profile,
        artifact_digests=artifact_digests,
    )
    return placeholder_measurements(
        "gcp",
        values,
        allow_placeholder=allow_placeholder,
        message="No measurement tool found for gcp measurements.",
        hint=HINT,
        context={"profile": profile},
    )
