"""Azure PCR-like measurement placeholder.

No tool integration exists: the values are SHA-256 digests of the artifact
digests, not PCRs any Azure confidential VM reports.
"""

from __future__ import annotations

from tundravm.measure.model import Measurements
from tundravm.measure.placeholder import digest_values, placeholder_measurements

HINT = (
    "Azure measurement derivation is a placeholder; use the cloud's attestation report "
    "to obtain real values."
)


def derive(
    profile: str,
    artifact_digests: dict[str, str],
    *,
    allow_placeholder: bool = False,
) -> Measurements:
    values = digest_values(
        ("PCR0", "PCR1", "PCR7"),
        salt="azure:",
        profile=profile,
        artifact_digests=artifact_digests,
    )
    return placeholder_measurements(
        "azure",
        values,
        allow_placeholder=allow_placeholder,
        message="No measurement tool found for azure measurements.",
        hint=HINT,
        context={"profile": profile},
    )
