import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tundravm import Image
from tundravm.backends import InProcessBackend
from tundravm.errors import StateError
from tundravm.measure import PlaceholderMeasurementWarning, rtmr

RTMR_A = "aa" * 48
RTMR_B = "bb" * 48
RTMR_C = "cc" * 48


def _measured_boot(name: str) -> str | None:
    return "/usr/bin/measured-boot" if name == "measured-boot" else None


def _image_with_backend(tmp_path: Path) -> Image:
    return Image(build_dir=tmp_path / "build", backend=InProcessBackend())


def test_measure_requires_baked_artifacts(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build")
    with pytest.raises(StateError, match="No bake result found"):
        image.measure(backend="rtmr")


def test_measure_supports_rtmr_azure_and_gcp(tmp_path: Path) -> None:
    image = _image_with_backend(tmp_path)
    image.targets("qemu")
    image.bake()

    with pytest.warns(PlaceholderMeasurementWarning):
        rtmr_measurements = image.measure(backend="rtmr", allow_placeholder=True)
        azure = image.measure(backend="azure", allow_placeholder=True)
        gcp = image.measure(backend="gcp", allow_placeholder=True)

    assert rtmr_measurements.backend == "rtmr"
    assert azure.backend == "azure"
    assert gcp.backend == "gcp"
    assert rtmr_measurements.values
    assert azure.values
    assert gcp.values


def test_measure_export_json_and_cbor_are_stable(tmp_path: Path) -> None:
    image = _image_with_backend(tmp_path)
    image.targets("qemu")
    image.bake()
    with pytest.warns(PlaceholderMeasurementWarning):
        measurements = image.measure(backend="rtmr", allow_placeholder=True)

    json_first = measurements.to_json()
    json_second = measurements.to_json()
    cbor_first = measurements.to_cbor()
    cbor_second = measurements.to_cbor()

    assert json_first == json_second
    assert cbor_first == cbor_second

    json_path = tmp_path / "measurements.json"
    cbor_path = tmp_path / "measurements.cbor"
    measurements.to_json(json_path)
    measurements.to_cbor(cbor_path)
    assert json_path.exists()
    assert cbor_path.exists()


def test_measure_verification_reports_actionable_mismatches(tmp_path: Path) -> None:
    image = _image_with_backend(tmp_path)
    image.targets("qemu")
    image.bake()
    with pytest.warns(PlaceholderMeasurementWarning):
        measurements = image.measure(backend="rtmr", allow_placeholder=True)

    result = measurements.verify(
        {
            "RTMR0": "00" * 32,
            "RTMR9": "11" * 32,
        },
    )

    reasons = {mismatch.reason for mismatch in result.mismatches}
    assert result.ok is False
    assert "value_mismatch" in reasons
    assert "missing_actual" in reasons


def test_rtmr_derive_uses_measured_boot_for_uki(tmp_path: Path) -> None:
    uki = tmp_path / "linux.efi"
    uki.write_bytes(b"uki")
    commands: list[list[str]] = []

    def fake_run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        if "--version" in command:
            return subprocess.CompletedProcess(list(command), 1, "", "unknown flag")
        Path(command[2]).write_text(
            f'{{"rtmr":{{"0":{{"expected":"{RTMR_A}"}},"1":{{"expected":"{RTMR_B}"}},'
            f'"2":{{"expected":"{RTMR_C}"}}}}}}',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(list(command), 0, "", "")

    measurements = rtmr.derive(
        "default", {str(uki): "deadbeef"}, (uki,), tool_locator=_measured_boot, runner=fake_run
    )

    assert commands[0] == ["/usr/bin/measured-boot", str(uki), commands[0][2], "--direct-uki"]
    assert measurements.values == {"RTMR0": RTMR_A, "RTMR1": RTMR_B, "RTMR2": RTMR_C}
    assert measurements.source == "measured-boot"
    assert measurements.tool_version is None
    assert measurements.artifact == str(uki)


def test_rtmr_derive_uses_measured_boot_for_disk_images(tmp_path: Path) -> None:
    disk = tmp_path / "image.raw"
    disk.write_bytes(b"raw")
    commands: list[list[str]] = []

    def fake_run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        if "--version" in command:
            return subprocess.CompletedProcess(list(command), 0, "measured-boot v0.4.2\n", "")
        Path(command[2]).write_text(f'{{"rtmr":{{"0":{{"expected":"{RTMR_A}"}}}}}}')
        return subprocess.CompletedProcess(list(command), 0, "", "")

    measurements = rtmr.derive(
        "default", {str(disk): "deadbeef"}, (disk,), tool_locator=_measured_boot, runner=fake_run
    )

    assert commands[0] == ["/usr/bin/measured-boot", str(disk), commands[0][2]]
    assert measurements.values == {"RTMR0": RTMR_A}
    assert measurements.tool_version == "v0.4.2"
