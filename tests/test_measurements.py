from pathlib import Path

import pytest

from tdx import Image
from tdx.errors import MeasurementError


def _image_with_backend(tmp_path: Path) -> Image:
    return Image(build_dir=tmp_path / "build", backend="inprocess")


def test_measure_requires_baked_artifacts() -> None:
    image = Image()
    with pytest.raises(MeasurementError):
        image.measure(backend="rtmr")


def test_measure_supports_rtmr_azure_and_gcp(tmp_path: Path) -> None:
    image = _image_with_backend(tmp_path)
    image.output_targets("qemu")
    image.bake()

    rtmr = image.measure(backend="rtmr")
    azure = image.measure(backend="azure")
    gcp = image.measure(backend="gcp")

    assert rtmr.backend == "rtmr"
    assert azure.backend == "azure"
    assert gcp.backend == "gcp"
    assert rtmr.values
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
