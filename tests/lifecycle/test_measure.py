import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tundravm.declarative import (
    Artifact,
    Backend,
    Fragment,
    Package,
    Recipe,
    bake,
    lock,
    measure,
    read_artifacts,
)
from tundravm.errors import StateError
from tundravm.measure import (
    MeasurementBackend,
    Measurements,
    PlaceholderMeasurementWarning,
    derive_measurements,
    rtmr,
)
from tundravm.models import ArtifactRef, ProfileBuildResult

RTMR_A = "aa" * 48
RTMR_B = "bb" * 48
RTMR_C = "cc" * 48


def _measured_boot(name: str) -> str | None:
    return "/usr/bin/measured-boot" if name == "measured-boot" else None


def _baked(tmp_path: Path) -> Artifact:
    recipe = Recipe(
        "measured", Fragment("measured", items=(Package("curl"), Package("linux-image-amd64")))
    )
    (artifact,) = bake(
        recipe, locked=lock(recipe), backend=Backend("inprocess"), out=tmp_path / "build"
    )
    return artifact


def _derived(artifact: Artifact, scheme: MeasurementBackend) -> Measurements:
    """The compiler's measurement record of *artifact* (exports and verification)."""
    profile = ProfileBuildResult(
        profile=artifact.variant,
        artifacts={artifact.target: ArtifactRef(target=artifact.target, path=artifact.path)},
    )
    return derive_measurements(
        backend=scheme, profile=artifact.variant, profile_result=profile, allow_placeholder=True
    )


def test_measure_requires_baked_artifacts(tmp_path: Path) -> None:
    with pytest.raises(StateError, match="No bake result found"):
        read_artifacts(tmp_path / "build")


def test_measure_supports_rtmr_azure_and_gcp(tmp_path: Path) -> None:
    artifact = _baked(tmp_path)

    with pytest.warns(PlaceholderMeasurementWarning):
        rtmr_measurements = measure(artifact, scheme="rtmr", allow_placeholder=True)
        azure = measure(artifact, scheme="azure", allow_placeholder=True)
        gcp = measure(artifact, scheme="gcp", allow_placeholder=True)

    assert rtmr_measurements.scheme == "rtmr"
    assert azure.scheme == "azure"
    assert gcp.scheme == "gcp"
    assert rtmr_measurements.values
    assert azure.values
    assert gcp.values
    assert {m.artifact_digest for m in (rtmr_measurements, azure, gcp)} == {artifact.sha256}


def test_measure_export_is_stable(tmp_path: Path) -> None:
    artifact = _baked(tmp_path)
    with pytest.warns(PlaceholderMeasurementWarning):
        first = measure(artifact, allow_placeholder=True)
        second = measure(artifact, allow_placeholder=True)

    json_path = tmp_path / "measurements.json"
    assert first.to_json(json_path) == second.to_json()
    assert json_path.read_text(encoding="utf-8") == first.to_json()
    with pytest.warns(PlaceholderMeasurementWarning):
        assert dict(first.values) == _derived(artifact, "rtmr").values


def test_measure_verification_reports_mismatched_registers(tmp_path: Path) -> None:
    artifact = _baked(tmp_path)
    with pytest.warns(PlaceholderMeasurementWarning):
        measurements = measure(artifact, allow_placeholder=True)
    values = dict(measurements.values)
    first = sorted(values)[0]

    assert measurements.verify(values) == ()
    assert measurements.verify({**values, first: "00" * 48, "RTMR9": "11" * 48}) == (
        first,
        "RTMR9",
    )


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
