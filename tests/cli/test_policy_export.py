"""``measure --export-policy`` and ``Tdxs.from_policy``: the measurement to verifier handoff."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT, run_main, write_recipe_file
from tundravm import MeasurementError, Measurements, ValidationError
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR
from tundravm.declarative import File
from tundravm.declarative.utils import Tdxs
from tundravm.measure.policy import PLACEHOLDER_NOTE, policy_payload, read_policy

RECIPE = """
from tundravm import Fragment, Package, Recipe, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
BOOT = (Package("systemd"), Package("linux-image-amd64"))
recipe = Recipe("peer", items=BOOT)
"""

R0, R1, R2, R3, MRTD = ("a" * 96, "b" * 96, "c" * 96, "d" * 96, "e" * 96)
QEMU_GAPS = {"MRTD": "host firmware"}


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


@pytest.fixture
def manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    recipe = write_recipe_file(tmp_path, RECIPE, monkeypatch)
    assert run_main("bake", str(recipe), "-q")[0] == EXIT_OK
    return tmp_path / "build"


def test_export_writes_a_marked_placeholder_policy(manifest: Path, tmp_path: Path) -> None:
    target = tmp_path / "peer.json"
    code, _ = run_main(
        "measure", "--build", str(manifest), "--allow-placeholder", "--export-policy", str(target)
    )
    assert code == EXIT_OK
    policy = json.loads(target.read_text(encoding="utf-8"))
    assert sorted(policy) == [
        "artifact",
        "boot_payload",
        "note",
        "registers",
        "schema",
        "schema_version",
        "scheme",
        "tool",
        "tool_version",
        "unverifiable",
    ]
    assert (policy["schema_version"], policy["schema"], policy["scheme"], policy["tool"]) == (
        2,
        "tundravm.policy/2",
        "qemu",
        "placeholder",
    )
    assert policy["tool_version"] is None
    assert policy["note"] == PLACEHOLDER_NOTE and policy["note"].startswith("PLACEHOLDER")
    assert policy["artifact"]["path"] == str(manifest / "default" / "disk.qcow2")
    assert len(policy["artifact"]["sha256"]) == 64
    assert policy["boot_payload"]["path"] == str(manifest / "default" / "disk.efi")
    assert sorted(policy["registers"]) == ["RTMR1", "RTMR2"]
    assert sorted(policy["unverifiable"]) == ["MRTD", "RTMR0"]
    assert read_policy(target)["registers"] == policy["registers"]


def test_export_refuses_placeholders_without_the_flag(
    manifest: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "peer.json"
    code, _ = run_main("measure", "--build", str(manifest), "--export-policy", str(target))
    assert code == EXIT_SDK_ERROR
    assert "E_MEASUREMENT" in capsys.readouterr().err
    assert not target.exists()
    with pytest.raises(MeasurementError, match="placeholder measurements as a verifier policy"):
        policy_payload(
            scheme="qemu",
            tool="placeholder",
            registers={"RTMR0": [R0], "RTMR1": [R1], "RTMR2": [R2]},
            unverifiable=QEMU_GAPS,
            artifact_path="x",
            artifact_sha256="f" * 64,
        )


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


def test_schema_1_policies_are_refused_with_a_re_export_hint() -> None:
    old = {**_policy(RTMR0=R0, RTMR1=R1, RTMR2=R2), "schema_version": 1, "scheme": "rtmr"}
    with pytest.raises(ValidationError, match="no longer read") as caught:
        read_policy(old)
    assert caught.value.hint is not None and "--export-policy" in caught.value.hint


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
    (config,) = [item for item in tdxs.items if isinstance(item, File)]
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


def test_from_policy_picks_one_value_per_register() -> None:
    firmware = [{"value": MRTD, "firmware": "fw-a"}, {"value": R3, "firmware": "fw-b"}]
    policy = _policy({}, MRTD=firmware, RTMR0=R0, RTMR1=R1, RTMR2=R2)
    with pytest.raises(MeasurementError, match="MRTD has 2 accepted values"):
        Tdxs.from_policy(policy)
    tdxs = Tdxs.from_policy(policy, firmware="fw-b")
    assert tdxs.expected_measurements["mrtd"] == R3
    assert tdxs.expected_measurements["rtmr0"] == R0
    (config,) = [item for item in tdxs.items if isinstance(item, File)]
    assert f'      mrtd: "{R3}"\n' in str(config.content)


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


def test_from_measurements_matches_from_policy() -> None:
    registers = (("RTMR0", (R0,)), ("RTMR1", (R1,)), ("RTMR2", (R2,)))
    measured = Measurements(
        scheme="qemu",
        values=(("RTMR0", R0), ("RTMR1", R1), ("RTMR2", R2)),
        tool="tundravm-measure 1.0",
        artifact_digest="f" * 64,
        registers=registers,
        unverifiable=tuple(QEMU_GAPS.items()),
    )
    assert Tdxs.from_measurements(measured) == Tdxs.from_policy(
        _policy(RTMR0=R0, RTMR1=R1, RTMR2=R2)
    )
    placeholder = Measurements(
        scheme="qemu",
        values=measured.values,
        tool="placeholder",
        artifact_digest="",
        registers=registers,
        unverifiable=measured.unverifiable,
    )
    with pytest.raises(MeasurementError):
        Tdxs.from_measurements(placeholder)


def test_the_committed_example_policy_is_a_marked_placeholder() -> None:
    policy = read_policy(REPO_ROOT / "examples" / "peer.policy.json")
    assert policy["tool"] == "placeholder" and policy["note"] == PLACEHOLDER_NOTE
    with pytest.raises(MeasurementError):
        Tdxs.from_policy(REPO_ROOT / "examples" / "peer.policy.json")


def test_export_keeps_the_operator_platform_approval(manifest: Path, tmp_path: Path) -> None:
    target = tmp_path / "peer.json"
    base = ("measure", "--build", str(manifest), "--allow-placeholder")
    approve = ("--approve-mrtd", MRTD.upper(), "--approve-rtmr0", R0)
    code, _ = run_main(*base, "--export-policy", str(target), *approve)
    assert code == EXIT_OK
    policy = json.loads(target.read_text(encoding="utf-8"))
    assert policy["platform_approval"] == {"MRTD": [MRTD], "RTMR0": [R0]}
    assert read_policy(target)["platform_approval"] == {"MRTD": [MRTD], "RTMR0": [R0]}
    assert run_main(*base, *approve)[0] == EXIT_SDK_ERROR  # no --export-policy to keep it in
    code, _ = run_main(*base, "--export-policy", str(target), "--approve-mrtd", "zz")
    assert code == EXIT_SDK_ERROR
