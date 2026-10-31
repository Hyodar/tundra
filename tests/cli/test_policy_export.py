"""Verifier policies (``policy_payload``, ``read_policy``) and ``Tdxs.from_policy``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT
from tundravm.declarative import File
from tundravm.declarative.utils import Tdxs
from tundravm.errors import MeasurementError, ValidationError
from tundravm.measure.policy import PLACEHOLDER_NOTE, policy_payload, read_policy

R0, R1, R2, R3, MRTD = ("a" * 96, "b" * 96, "c" * 96, "d" * 96, "e" * 96)
QEMU_GAPS = {"MRTD": "host firmware"}
CONFIG = "/etc/tdxs/config.yaml"


def _policy(unverifiable: dict[str, str] | None = None, **registers: object) -> dict[str, object]:
    """A schema 2 qemu policy; a register given as a string accepts that one value."""
    return {
        "schema_version": 2,
        "schema": "tundravm.policy/2",
        "scheme": "qemu",
        "tool": "tundravm-measure",
        "tool_version": "1.0",
        "artifact": {"path": "build/default/image.efi", "sha256": "f" * 64},
        "registers": {k: [v] if isinstance(v, str) else v for k, v in registers.items()},
        "unverifiable": QEMU_GAPS if unverifiable is None else unverifiable,
    }


def test_export_rejects_uncovered_registers_and_unknown_schemes() -> None:
    with pytest.raises(ValidationError, match="neither accepted nor unverifiable"):
        policy_payload(
            scheme="qemu",
            tool="tundravm-measure 1.0",
            registers={"RTMR1": [R1], "RTMR2": [R2]},
            artifact_path="x",
            artifact_sha256="f" * 64,
        )
    with pytest.raises(ValidationError, match="accepted and unverifiable"):
        policy_payload(
            scheme="qemu",
            tool="tundravm-measure 1.0",
            registers={"MRTD": [MRTD], "RTMR0": [R0], "RTMR1": [R1], "RTMR2": [R2]},
            unverifiable=QEMU_GAPS,
            artifact_path="x",
            artifact_sha256="f" * 64,
        )
    with pytest.raises(ValidationError, match="'rtmr' measurements"):
        policy_payload(
            scheme="rtmr",
            tool="tundravm-measure 1.0",
            registers={},
            artifact_path="x",
            artifact_sha256="",
        )
    payload = policy_payload(
        scheme="qemu",
        tool="tundravm-measure 1.0",
        registers={"rtmr0": [R0.upper()], "RTMR1": [R1], "RTMR2": [R2], "RTMR3": [R3]},
        unverifiable=QEMU_GAPS,
        artifact_path="x.efi",
        artifact_sha256="f" * 64,
    )
    assert (payload["tool"], payload["tool_version"]) == ("tundravm-measure", "1.0")
    assert payload["registers"] == {"RTMR0": [R0], "RTMR1": [R1], "RTMR2": [R2], "RTMR3": [R3]}
    assert payload["unverifiable"] == QEMU_GAPS
    assert "note" not in payload


def test_labelled_values_keep_their_labels() -> None:
    entry = {"value": MRTD.upper(), "firmware": "gcp-ovmf-1", "other": "dropped"}
    policy = read_policy(_policy({}, MRTD=[entry, R3], RTMR0=R0, RTMR1=R1, RTMR2=R2))
    assert policy["registers"]["MRTD"] == [{"value": MRTD, "firmware": "gcp-ovmf-1"}, R3]  # type: ignore[index]
    with pytest.raises(ValidationError, match="non-empty list"):
        read_policy(_policy(RTMR0=[], RTMR1=R1, RTMR2=R2))


def test_from_policy_normalises_registers_for_the_validator() -> None:
    tdxs = Tdxs.from_policy(_policy(RTMR0=R0, RTMR1=R1, RTMR2=R2, RTMR3=R3), mrtd=MRTD.upper())
    assert tdxs.validator == "tdx"
    assert tdxs.expected_measurements == {
        "mrtd": MRTD,
        "rtmr0": R0,
        "rtmr1": R1,
        "rtmr2": R2,
        "rtmr3": R3,
    }
    (config,) = [item for item in tdxs.items if isinstance(item, File) and item.path == CONFIG]
    assert config.content == (
        "transport:\n  type: socket\n  config:\n    systemd: true\n"
        "issuer:\n  type: tdx\n"
        "validator:\n  type: tdx\n  config:\n"
        "    expected_measurements:\n"
        f'      mrtd: "{MRTD}"\n'
        f'      rtmr0: "{R0}"\n'
        f'      rtmr1: "{R1}"\n'
        f'      rtmr2: "{R2}"\n'
        f'      rtmr3: "{R3}"\n'
    )


def test_from_policy_reads_a_file_and_passes_fields(tmp_path: Path) -> None:
    path = tmp_path / "peer.json"
    path.write_text(json.dumps(_policy(RTMR0=R0, RTMR1=R1, RTMR2=R2)), encoding="utf-8")
    tdxs = Tdxs.from_policy(path, validator="azure", issuer="azure", verify_imds=True)
    assert (tdxs.validator, tdxs.issuer, tdxs.verify_imds) == ("azure", "azure", True)
    assert dict(tdxs.expected_measurements) == {"rtmr0": R0, "rtmr1": R1, "rtmr2": R2}


def test_from_policy_rejects_bad_policies(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="neither accepted nor unverifiable"):
        Tdxs.from_policy(_policy(RTMR0=R0, RTMR1=R1))
    with pytest.raises(ValidationError, match="schema_version"):
        Tdxs.from_policy({**_policy(RTMR0=R0, RTMR1=R1, RTMR2=R2), "schema_version": 9})
    with pytest.raises(ValidationError, match="not a SHA-384 digest"):
        Tdxs.from_policy(_policy(RTMR0="zz", RTMR1=R1, RTMR2=R2))
    with pytest.raises(ValidationError, match="Cannot read policy"):
        Tdxs.from_policy(tmp_path / "missing.json")
    with pytest.raises(ValidationError, match="not a SHA-384 digest"):
        Tdxs.from_policy(_policy(RTMR0=R0, RTMR1=R1, RTMR2=R2), mrtd="short")
    with pytest.raises(ValidationError, match="takes the measurements from the policy"):
        Tdxs.from_policy(_policy(RTMR0=R0, RTMR1=R1, RTMR2=R2), expected_measurements=())
    placeholder = {**_policy(RTMR0="1" * 64, RTMR1="2" * 64, RTMR2="3" * 64), "tool": "placeholder"}
    with pytest.raises(MeasurementError, match="placeholder policy"):
        Tdxs.from_policy(placeholder)
    assert Tdxs.from_policy(placeholder, allow_placeholder=True).expected_measurements["rtmr0"] == (
        "1" * 64
    )


def test_the_committed_example_policy_is_a_marked_placeholder() -> None:
    policy = read_policy(REPO_ROOT / "examples" / "peer.policy.json")
    assert policy["tool"] == "placeholder" and policy["note"] == PLACEHOLDER_NOTE
    with pytest.raises(MeasurementError):
        Tdxs.from_policy(REPO_ROOT / "examples" / "peer.policy.json")
