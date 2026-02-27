from pathlib import Path

import pytest

from tundravm import Image
from tundravm.backends import InProcessBackend
from tundravm.errors import MeasurementError
from tundravm.measure import rtmr


class _FakeRunResult:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def _image_with_backend(tmp_path: Path) -> Image:
    return Image(build_dir=tmp_path / "build", backend=InProcessBackend())


def test_measure_requires_baked_artifacts() -> None:
    image = Image()
    with pytest.raises(MeasurementError):
        image.measure(backend="rtmr")


def test_measure_supports_rtmr_azure_and_gcp(tmp_path: Path) -> None:
    image = _image_with_backend(tmp_path)
    image.output_targets("qemu")
    image.bake()

    rtmr_measurements = image.measure(backend="rtmr")
    azure = image.measure(backend="azure")
    gcp = image.measure(backend="gcp")

    assert rtmr_measurements.backend == "rtmr"
    assert azure.backend == "azure"
    assert gcp.backend == "gcp"
    assert rtmr_measurements.values
    assert azure.values
    assert gcp.values


def test_measure_export_json_and_cbor_are_stable(tmp_path: Path) -> None:
    image = _image_with_backend(tmp_path)
    image.output_targets("qemu")
    image.bake()
    measurements = image.measure(backend="rtmr")

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
    image.output_targets("qemu")
    image.bake()
    measurements = image.measure(backend="rtmr")

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


def test_rtmr_derive_uses_measured_boot_for_uki(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    uki = tmp_path / "linux.efi"
    uki.write_bytes(b"uki")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> _FakeRunResult:
        commands.append(command)
        output_path = Path(command[2])
        output_path.write_text(
            '{"rtmr":{"0":{"expected":"aa"},"1":{"expected":"bb"},"2":{"expected":"cc"}}}',
            encoding="utf-8",
        )
        return _FakeRunResult()

    monkeypatch.setattr(
        "tundravm.measure.rtmr.shutil.which",
        lambda name: "/usr/bin/measured-boot" if name == "measured-boot" else None,
    )
    monkeypatch.setattr("tundravm.measure.rtmr.subprocess.run", fake_run)

    values = rtmr.derive("default", {str(uki): "deadbeef"}, (uki,))

    assert commands == [["/usr/bin/measured-boot", str(uki), commands[0][2], "--direct-uki"]]
    assert values == {"RTMR0": "aa", "RTMR1": "bb", "RTMR2": "cc"}


def test_rtmr_derive_uses_measured_boot_for_disk_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    disk = tmp_path / "image.raw"
    disk.write_bytes(b"raw")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> _FakeRunResult:
        commands.append(command)
        output_path = Path(command[2])
        output_path.write_text('{"rtmr":{"0":{"expected":"ff"}}}', encoding="utf-8")
        return _FakeRunResult()

    monkeypatch.setattr(
        "tundravm.measure.rtmr.shutil.which",
        lambda name: "/usr/bin/measured-boot" if name == "measured-boot" else None,
    )
    monkeypatch.setattr("tundravm.measure.rtmr.subprocess.run", fake_run)

    values = rtmr.derive("default", {str(disk): "deadbeef"}, (disk,))

    assert commands == [["/usr/bin/measured-boot", str(disk), commands[0][2]]]
    assert values == {"RTMR0": "ff"}
