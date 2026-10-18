"""``measure`` schemes on real (synthetic) UKIs and disks, and on simulated artifacts."""

import hashlib
import struct
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from tundravm.declarative import (
    Artifact,
    Backend,
    Package,
    Recipe,
    bake,
    lock,
    measure,
    read_artifacts,
)
from tundravm.declarative.utils import Tdxs
from tundravm.errors import MeasurementError, StateError
from tundravm.measure import (
    MEASURE_TOOL,
    MEASURE_VERSION,
    MeasurementBackend,
    Measurements,
    PlaceholderMeasurementWarning,
    derive_measurements,
    schemes,
)
from tundravm.measure.policy import read_policy
from tundravm.models import ArtifactRef, ProfileBuildResult

_ALIGN = 0x200
ALL_SCHEMES: tuple[MeasurementBackend, ...] = ("qemu", "gcp", "azure", "portable")
ESP_TYPE = bytes.fromhex("28732ac11ff8d211ba4b00a0c93ec93b")


def _align(size: int) -> int:
    return (size + _ALIGN - 1) // _ALIGN * _ALIGN


def build_pe(sections: Sequence[tuple[str, bytes]]) -> bytes:
    """A minimal valid PE32+ image holding *sections* (name, data)."""
    count = len(sections)
    size_of_headers = _align(0x40 + 24 + 240 + 40 * count)
    offset = size_of_headers
    table = b""
    body = b""
    for name, data in sections:
        raw = _align(len(data))
        table += struct.pack(
            "<8sIIIIIIHHI", name.encode(), len(data), offset, raw, offset, 0, 0, 0, 0, 0x40000040
        )
        body += data.ljust(raw, b"\0")
        offset += raw
    dos = b"MZ".ljust(0x3C, b"\0") + struct.pack("<I", 0x40)
    coff = b"PE\0\0" + struct.pack("<HHIIIHH", 0x8664, count, 0, 0, 0, 240, 0x22)
    optional = bytearray(240)
    struct.pack_into("<H", optional, 0, 0x20B)
    struct.pack_into("<I", optional, 60, size_of_headers)
    struct.pack_into("<I", optional, 108, 16)
    head = (dos + coff + bytes(optional) + table).ljust(size_of_headers, b"\0")
    return head + body


def build_uki(cmdline: bytes = b"console=ttyS0", stub: int = 257) -> bytes:
    """A UKI: stub text, ``.sdmagic``, ``.osrel``, ``.cmdline``, ``.initrd``, ``.linux`` (a PE)."""
    linux = build_pe([(".text", b"kernel")])
    return build_pe(
        [
            (".text", b"stub"),
            (".sdmagic", f"#### LoaderInfo: systemd-stub {stub} ####".encode()),
            (".osrel", b"ID=test\n"),
            (".cmdline", cmdline),
            (".initrd", b"initrd"),
            (".linux", linux),
        ]
    )


def build_disk_head(disk_guid: bytes = b"\x12" * 16) -> bytes:
    """The first 34 sectors of a GPT disk with one ESP entry."""
    head = bytearray(34 * 512)
    header = bytearray(92)
    header[:8] = b"EFI PART"
    header[56:72] = disk_guid
    struct.pack_into("<II", header, 80, 128, 128)
    head[512 : 512 + 92] = header
    head[1024 : 1024 + 16] = ESP_TYPE
    struct.pack_into("<QQ", head, 1024 + 32, 2048, 4095)
    return bytes(head)


@pytest.fixture
def uki(tmp_path: Path) -> Path:
    path = tmp_path / "linux.efi"
    path.write_bytes(build_uki())
    return path


@pytest.fixture
def disk(tmp_path: Path) -> Path:
    path = tmp_path / "disk.raw"
    path.write_bytes(build_disk_head() + bytes(4096))
    return path


def _baked(tmp_path: Path) -> Artifact:
    recipe = Recipe("measured", items=(Package("curl"), Package("linux-image-amd64")))
    (artifact,) = bake(
        recipe, lock=lock(recipe), backend=Backend("inprocess"), build=tmp_path / "build"
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


def _hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 96 and int(value, 16) >= 0


def test_measure_requires_baked_artifacts(tmp_path: Path) -> None:
    with pytest.raises(StateError, match="No bake result found"):
        read_artifacts(tmp_path / "build")


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


def test_the_command_line_and_stub_version_reach_rtmr2(tmp_path: Path, uki: Path) -> None:
    other = tmp_path / "other.efi"
    other.write_bytes(build_uki(cmdline=b"console=ttyS1"))
    old_stub = tmp_path / "old.efi"
    old_stub.write_bytes(build_uki(stub=255))
    rtmr2 = {
        path.name: schemes.derive("portable", artifact=path, uki=None).registers["RTMR2"]
        for path in (uki, other, old_stub)
    }
    assert len({value[0] for value in rtmr2.values()}) == 3


def test_a_disk_without_its_uki_is_refused(disk: Path) -> None:
    with pytest.raises(MeasurementError, match="recorded no UKI"):
        schemes.derive("qemu", artifact=disk, uki=None)


def test_measure_of_a_real_uki_round_trips_through_the_policy(tmp_path: Path, uki: Path) -> None:
    artifact = replace(
        _baked(tmp_path),
        path=uki,
        sha256=hashlib.sha256(uki.read_bytes()).hexdigest(),
        simulated=False,
    )
    found = measure(artifact, scheme="qemu")

    policy = found.to_policy()
    assert found.tool == f"{MEASURE_TOOL} {MEASURE_VERSION}"
    assert policy["schema_version"] == 2 and policy["schema"] == "tundravm.policy/2"
    unverifiable = policy["unverifiable"]
    assert isinstance(unverifiable, dict) and set(unverifiable) == {"MRTD", "RTMR0"}
    assert read_policy(policy) == policy
    assert dict(found.values) == {k: v[0] for k, v in found.registers}
    assert set(dict(Tdxs.from_policy(policy).expected_measurements)) == {"rtmr1", "rtmr2"}


def test_simulated_artifacts_give_placeholders_for_every_scheme(tmp_path: Path) -> None:
    artifact = _baked(tmp_path)

    with pytest.warns(PlaceholderMeasurementWarning):
        found = {
            scheme: measure(artifact, scheme=scheme, allow_placeholder=True)
            for scheme in ALL_SCHEMES
        }

    assert {m.tool for m in found.values()} == {"placeholder"}
    assert {m.artifact_digest for m in found.values()} == {artifact.sha256}
    assert set(dict(found["qemu"].values)) == {"RTMR1", "RTMR2"}
    assert len(dict(found["gcp"].registers)["MRTD"]) == 3
    assert found["azure"].registers == ()
    assert set(dict(found["azure"].unverifiable)) == {"MRTD", "RTMR0", "RTMR1", "RTMR2"}


def test_measure_export_is_stable(tmp_path: Path) -> None:
    artifact = _baked(tmp_path)
    with pytest.warns(PlaceholderMeasurementWarning):
        first = measure(artifact, allow_placeholder=True)
        second = measure(artifact, allow_placeholder=True)

    assert first.to_dict() == second.to_dict()
    assert first.to_dict()["artifact"] == str(artifact.path)
    assert first.to_policy(allow_placeholder=True) == second.to_policy(allow_placeholder=True)
    with pytest.warns(PlaceholderMeasurementWarning):
        assert dict(first.registers) == {
            k: tuple(v) for k, v in _derived(artifact, "qemu").registers.items()
        }


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
