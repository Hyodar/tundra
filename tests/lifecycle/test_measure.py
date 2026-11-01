"""``measure`` schemes on real (synthetic) UKIs and disks; an artifact that is no UKI."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from tundravm.declarative import (
    Artifact,
    Backend,
    Package,
    Recipe,
    Variant,
    bake,
    lock,
    measure,
)
from tundravm.declarative.utils import Tdxs
from tundravm.errors import MeasurementError
from tundravm.measure import (
    MEASURE_TOOL,
    MEASURE_VERSION,
    MeasurementBackend,
    Measurements,
    derive_measurements,
    schemes,
)
from tundravm.measure.policy import read_policy
from tundravm.models import ArtifactRef, ProfileBuildResult
from tundravm.testing._fake_mkosi import fake_disk, fake_uki

ALL_SCHEMES: tuple[MeasurementBackend, ...] = ("qemu", "gcp", "azure", "portable")


@pytest.fixture
def uki(tmp_path: Path) -> Path:
    path = tmp_path / "linux.efi"
    path.write_bytes(fake_uki())
    return path


@pytest.fixture
def disk(tmp_path: Path) -> Path:
    path = tmp_path / "disk.raw"
    path.write_bytes(fake_disk() + bytes(4096))
    return path


def _baked(tmp_path: Path) -> Artifact:
    """The qemu artifact of a build against the fake mkosi."""
    recipe = Recipe("measured", items=(Package("curl"), Package("linux-image-amd64")))
    (artifact,) = bake(
        recipe, lock=lock(recipe), backend=Backend("local"), build=tmp_path / "build"
    )
    return artifact


def _measurable(tmp_path: Path, uki: Path) -> Artifact:
    """A baked artifact whose file is the synthetic UKI *uki*."""
    return replace(_baked(tmp_path), path=uki, sha256=hashlib.sha256(uki.read_bytes()).hexdigest())


def _derived(artifact: Artifact, scheme: MeasurementBackend) -> Measurements:
    """The compiler's measurement record of *artifact* (exports and verification)."""
    profile = ProfileBuildResult(
        profile=artifact.profile,
        artifacts={artifact.target: ArtifactRef(target=artifact.target, path=artifact.path)},
    )
    return derive_measurements(backend=scheme, profile=artifact.profile, profile_result=profile)


def _hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 96 and int(value, 16) >= 0


@pytest.mark.parametrize("scheme", ["qemu", "portable"])
def test_image_schemes_predict_rtmr1_and_rtmr2_only(uki: Path, scheme: MeasurementBackend) -> None:
    found = schemes.derive(scheme, artifact=uki, uki=None)

    assert set(found.registers) == {"RTMR1", "RTMR2"}
    assert all(len(v) == 1 and _hex(v[0]) for v in found.registers.values())
    assert set(found.unverifiable) == {"MRTD", "RTMR0"}
    assert found.source == MEASURE_TOOL and found.tool_version == MEASURE_VERSION
    assert found.artifact == str(uki)


def test_gcp_accepts_every_published_firmware(uki: Path, disk: Path) -> None:
    found = schemes.derive("gcp", artifact=disk, uki=uki)

    mrtd, rtmr0 = found.registers["MRTD"], found.registers["RTMR0"]
    assert len(mrtd) == 3 and len(rtmr0) == 27
    assert all(isinstance(e, dict) and e["firmware"].startswith("gcp-ovmf-") for e in mrtd)
    assert all(isinstance(e, dict) and e["firmware"] and e["label"] for e in rtmr0)
    assert {e["firmware"] for e in rtmr0 if isinstance(e, dict)} == {
        e["firmware"] for e in mrtd if isinstance(e, dict)
    }
    assert len(found.registers["RTMR1"]) == len(found.registers["RTMR2"]) == 1
    assert found.unverifiable == {}
    assert len(found.extra["sources"]) == 3  # type: ignore[arg-type]
    assert str(found.extra["firmware_table"]).startswith("dstack-mr-gcp@")


def test_gcp_needs_the_disk_it_boots_from(uki: Path) -> None:
    with pytest.raises(MeasurementError, match="GCP boots from a disk"):
        schemes.derive("gcp", artifact=uki, uki=None)


def test_azure_predicts_no_tdx_register_but_records_vtpm_pcrs(uki: Path) -> None:
    found = schemes.derive("azure", artifact=uki, uki=None)

    assert found.registers == {}
    assert set(found.unverifiable) == {"MRTD", "RTMR0", "RTMR1", "RTMR2"}
    assert all("paravisor" in reason for reason in found.unverifiable.values())
    pcrs = found.extra["vtpm_pcrs"]
    assert isinstance(pcrs, dict) and set(pcrs) == {"PCR4", "PCR9", "PCR11"}
    assert all(len(value) == 64 for value in pcrs.values())


def test_booting_from_a_disk_changes_rtmr1_not_rtmr2(uki: Path, disk: Path) -> None:
    bare = schemes.derive("qemu", artifact=uki, uki=None)
    booted = schemes.derive("qemu", artifact=disk, uki=uki)

    assert bare.registers["RTMR1"] != booted.registers["RTMR1"]
    assert bare.registers["RTMR2"] == booted.registers["RTMR2"]


def test_a_disk_without_its_uki_is_refused(disk: Path) -> None:
    with pytest.raises(MeasurementError, match="recorded no UKI"):
        schemes.derive("qemu", artifact=disk, uki=None)


def test_measure_of_a_real_uki_round_trips_through_the_policy(tmp_path: Path, uki: Path) -> None:
    found = measure(_measurable(tmp_path, uki), scheme="qemu")

    policy = found.to_policy()
    assert found.tool == f"{MEASURE_TOOL} {MEASURE_VERSION}"
    assert policy["schema_version"] == 2 and policy["schema"] == "tundravm.policy/2"
    unverifiable = policy["unverifiable"]
    assert isinstance(unverifiable, dict) and set(unverifiable) == {"MRTD", "RTMR0"}
    assert read_policy(policy) == policy
    assert dict(found.values) == {k: v[0] for k, v in found.registers}
    assert set(dict(Tdxs.from_policy(policy).expected_measurements)) == {"rtmr1", "rtmr2"}


def _no_uki(tmp_path: Path, variant: str) -> Artifact:
    """The *variant* artifact of a qemu ``default`` and a ``gcp`` variant built against the
    fake mkosi, its boot payload overwritten with bytes that are no UKI."""
    recipe = Recipe(
        "measured",
        items=(Package("curl"), Package("linux-image-amd64")),
        variants=(Variant("default", outputs=("qemu",)), Variant("gcp", outputs=("gcp",))),
    )
    built = bake(recipe, lock=lock(recipe), backend=Backend("local"), build=tmp_path / "build")
    artifact = next(a for a in built if a.profile == variant)
    assert artifact.boot_payload is not None
    Path(artifact.boot_payload).write_bytes(b"no UKI")
    return replace(artifact, boot_payload_sha256=hashlib.sha256(b"no UKI").hexdigest())


@pytest.mark.parametrize(
    ("variant", "scheme"),
    [
        *(("default", scheme) for scheme in ALL_SCHEMES),
        *(("gcp", scheme) for scheme in ("gcp", "azure", "portable")),
    ],
    ids=lambda value: str(value),
)
def test_an_artifact_that_is_no_uki_is_refused_for_every_scheme(
    tmp_path: Path, variant: str, scheme: MeasurementBackend
) -> None:
    """A qemu placeholder or a gcp disk artifact without a UKI: no scheme measures it, and
    the error says the boot payload is no PE/EFI image and hints at a UKI."""
    with pytest.raises(MeasurementError) as caught:
        measure(_no_uki(tmp_path, variant), scheme=scheme)

    error = caught.value
    assert error.code == "E_MEASUREMENT"
    assert "is not a PE/EFI image" in str(error)
    assert error.hint is not None and "UKI (.efi)" in error.hint


def test_measure_export_is_stable(tmp_path: Path, uki: Path) -> None:
    artifact = _measurable(tmp_path, uki)
    first = measure(artifact)
    second = measure(artifact)

    assert first.to_dict() == second.to_dict()
    assert first.to_dict()["artifact"] == str(artifact.path)
    assert first.to_policy() == second.to_policy()
    assert dict(first.registers) == {
        k: tuple(v) for k, v in _derived(artifact, "qemu").registers.items()
    }
