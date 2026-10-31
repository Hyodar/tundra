"""``tundravm.policy/2``: multi-value registers, unverifiable registers, the validator config."""

from __future__ import annotations

import pytest

from tundravm.declarative.utils import Tdxs
from tundravm.errors import MeasurementError, ValidationError
from tundravm.measure.policy import (
    policy_payload,
)

A, B, C, D = ("a" * 96, "b" * 96, "c" * 96, "d" * 96)


def test_a_list_is_refused_as_an_expected_measurement() -> None:
    with pytest.raises(ValidationError, match="one hex string"):
        Tdxs(expected_measurements={"rtmr0": (A, B)}).compose()  # type: ignore[dict-item]


def test_an_azure_policy_builds_no_validator() -> None:
    gaps = dict.fromkeys(("MRTD", "RTMR0", "RTMR1", "RTMR2"), "paravisor")
    policy = policy_payload(
        scheme="azure",
        tool="tundravm-measure 1.0",
        registers={},
        unverifiable=gaps,
        artifact_path="disk.vhd",
        artifact_sha256="0" * 64,
    )
    with pytest.raises(MeasurementError, match="accepts no TDX register"):
        Tdxs.from_policy(policy)
