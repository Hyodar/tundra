"""``tundravm.policy/2``: multi-value registers, unverifiable registers, the validator config."""

from __future__ import annotations

import pytest

from tundravm._modules.tdxs import Tdxs as TdxsSpec
from tundravm.declarative.utils import Tdxs
from tundravm.errors import MeasurementError, ValidationError
from tundravm.measure.policy import (
    match_registers,
    policy_payload,
    read_policy,
    validator_measurements,
)

A, B, C, D = ("a" * 96, "b" * 96, "c" * 96, "d" * 96)
QEMU_GAPS = {"MRTD": "host firmware", "RTMR0": "host firmware"}


def _policy(registers: dict[str, list[object]], gaps: dict[str, str]) -> dict[str, object]:
    return policy_payload(
        scheme="gcp",
        tool="tundravm-measure 1.0",
        registers=registers,  # type: ignore[arg-type]
        unverifiable=gaps,
        artifact_path="disk.tar.gz",
        artifact_sha256="0" * 64,
    )


def test_a_register_matches_any_accepted_value_and_names_its_label() -> None:
    policy = _policy(
        {
            "MRTD": [{"value": A, "firmware": "fw-1"}, {"value": B, "firmware": "fw-2"}],
            "RTMR0": [{"value": C, "firmware": "fw-2", "label": "c3-standard-4"}],
            "RTMR1": [D],
            "RTMR2": [D],
        },
        {},
    )
    found = match_registers(policy["registers"], {"MRTD": B, "RTMR0": C, "RTMR1": D, "RTMR2": D})  # type: ignore[arg-type]
    assert found["MRTD"][0] == "match" and found["MRTD"][1] is not None
    assert found["RTMR0"][1] is not None and found["RTMR0"][1].describe() == "fw-2 c3-standard-4"
    assert found["RTMR3"] == ("unchecked", None)


def test_mrtd_and_rtmr0_of_different_firmwares_do_not_pair() -> None:
    policy = _policy(
        {
            "MRTD": [{"value": A, "firmware": "fw-1"}],
            "RTMR0": [{"value": C, "firmware": "fw-2"}],
            "RTMR1": [D],
            "RTMR2": [D],
        },
        {},
    )
    found = match_registers(policy["registers"], {"MRTD": A, "RTMR0": C, "RTMR1": D, "RTMR2": D})  # type: ignore[arg-type]
    assert found["RTMR0"] == ("mismatch", None)


def test_every_covered_register_is_accepted_or_unverifiable() -> None:
    with pytest.raises(ValidationError, match="neither accepted nor unverifiable"):
        _policy({"RTMR1": [D], "RTMR2": [D]}, {"MRTD": "host firmware"})
    with pytest.raises(ValidationError, match="accepted and unverifiable"):
        _policy({"MRTD": [A], "RTMR0": [A], "RTMR1": [D], "RTMR2": [D]}, QEMU_GAPS)
    with pytest.raises(ValidationError, match="non-empty list"):
        _policy({"RTMR0": [], "RTMR1": [D], "RTMR2": [D]}, {"MRTD": "x"})


def test_a_schema_1_policy_is_refused_with_a_re_export_hint() -> None:
    old = {"schema_version": 1, "scheme": "rtmr", "registers": {"RTMR0": A}}
    with pytest.raises(ValidationError, match="schema_version 1") as caught:
        read_policy(old)
    assert "--export-policy" in str(caught.value.hint)


def test_the_validator_gets_one_value_per_register() -> None:
    policy = _policy({"RTMR0": [A, B], "RTMR1": [D], "RTMR2": [D]}, {"MRTD": "host"})
    with pytest.raises(MeasurementError, match="one value per register") as caught:
        validator_measurements(policy["registers"])  # type: ignore[arg-type]
    assert "firmware=" in str(caught.value.hint)
    with pytest.raises(MeasurementError, match="RTMR0 has 2 accepted values"):
        Tdxs.from_policy(policy)


def test_firmware_and_label_pick_the_values_for_the_validator() -> None:
    rtmr0: list[object] = [
        {"value": A, "firmware": "fw-a", "label": "c3-standard-4"},
        {"value": B, "firmware": "fw-b", "label": "c3-standard-4"},
    ]
    policy = _policy({"RTMR0": rtmr0, "RTMR1": [D], "RTMR2": [D]}, {"MRTD": "host"})
    expected = dict(validator_measurements(policy["registers"], firmware="fw-b"))  # type: ignore[arg-type]
    assert expected == {"rtmr0": B, "rtmr1": D, "rtmr2": D}
    config = TdxsSpec(validator_type="tdx", expected_measurements=expected).render_config()
    assert f'      rtmr0: "{B}"' in config
    assert Tdxs.from_policy(policy, firmware="fw-a").expected_measurements["rtmr0"] == A


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
