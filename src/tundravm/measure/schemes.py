"""Per-scheme measurement: what each host family's TDX quote should hold for an image.

``qemu`` and ``portable`` predict RTMR1/RTMR2 (boot from the artifact's disk, or from
the UKI directly); MRTD/RTMR0 depend on the host's TDVF and QEMU, so they are
unverifiable. ``gcp`` adds every MRTD/RTMR0 Google's published firmwares produce
(:mod:`gcp_firmware`). ``azure`` cannot predict any TDX register (Azure's paravisor owns
the TD); it records the vTPM PCR4/9/11 the guest's boot extends instead.
"""

from __future__ import annotations

import gzip
import hashlib
import tarfile
from pathlib import Path

from tundravm.errors import MeasurementError
from tundravm.measure import gcp_firmware, tdx
from tundravm.measure.model import (
    MEASURE_VERSION,
    MeasurementBackend,
    Measurements,
)
from tundravm.measure.placeholder import placeholder_measurements

DISK_SUFFIXES = frozenset({".raw", ".img", ".vhd", ".qcow2", ".gz"})
MEASURABLE_SUFFIXES = DISK_SUFFIXES | {".efi"}
"""Artifacts ``measure`` reads: a UKI, or a disk image whose UKI the bake recorded."""
_HEAD = 34 * 512
HOST_FIRMWARE = (
    "depends on the host's TDVF/OVMF build, its ACPI tables and the VM's memory layout; "
    "take it from a quote of a host you trust"
)
REASONS: dict[str, dict[str, str]] = {
    "qemu": {"MRTD": f"QEMU: {HOST_FIRMWARE}", "RTMR0": f"QEMU: {HOST_FIRMWARE}"},
    "portable": {
        "MRTD": "portable: firmware register, left to the platform's own verifier",
        "RTMR0": "portable: firmware register, left to the platform's own verifier",
    },
    "azure": {
        name: "Azure: the paravisor owns the TD, so the TDX quote measures Azure's "
        "firmware, not this image; the image is measured into the vTPM (vtpm_pcrs)"
        for name in ("MRTD", "RTMR0", "RTMR1", "RTMR2")
    },
    "gcp": {},
}


def measurable(path: str | Path) -> bool:
    """Whether ``measure`` reads the artifact at *path*, by its suffix."""
    return Path(path).suffix in MEASURABLE_SUFFIXES


def derive(
    scheme: MeasurementBackend,
    *,
    artifact: Path,
    uki: Path | None,
    allow_placeholder: bool = False,
) -> Measurements:
    """Measure *artifact* (booting the UKI *uki*) for *scheme*."""
    if scheme not in REASONS:
        raise MeasurementError(
            "Unsupported measurement scheme.",
            hint=f"Use one of: {', '.join(REASONS)}",
            context={"scheme": scheme},
        )
    try:
        image = tdx.image_hashes(_read_uki(artifact, uki))
        gpt = _gpt_event(artifact)
    except MeasurementError as exc:
        if not allow_placeholder:
            raise
        return _placeholder(scheme, artifact, str(exc))
    registers: dict[str, list[str | dict[str, str]]] = {}
    extra: dict[str, object] = {}
    if scheme == "gcp":
        if gpt is None:
            raise MeasurementError(
                "GCP boots from a disk: measure the gcp artifact, not a bare UKI.",
                hint="Add the gcp output target and bake again.",
                context={"artifact": str(artifact)},
            )
        registers |= _gcp_firmware_registers()
        extra = _gcp_extra()
    if scheme == "azure":
        extra = {"vtpm_pcrs": tdx.vtpm_pcrs(_read_uki(artifact, uki))}
    else:
        registers["RTMR1"] = [image.rtmr1(gpt).hex()]
        registers["RTMR2"] = [image.rtmr2().hex()]
    return Measurements(
        scheme,
        registers,
        dict(REASONS[scheme]),
        extra,
        source="tundravm-measure",
        tool_version=MEASURE_VERSION,
        artifact=str(artifact),
    )


def _gcp_firmware_registers() -> dict[str, list[str | dict[str, str]]]:
    mrtd: list[str | dict[str, str]] = [
        {"value": fw.mrtd, "firmware": fw.label} for fw in gcp_firmware.FIRMWARES
    ]
    rtmr0: list[str | dict[str, str]] = [
        {"value": value, "firmware": firmware, "label": label}
        for value, firmware, label in tdx.gcp_rtmr0()
    ]
    return {"MRTD": mrtd, "RTMR0": rtmr0}


def _gcp_extra() -> dict[str, object]:
    return {
        "firmware_table": gcp_firmware.TABLE_VERSION,
        "sources": [
            gcp_firmware.FIRMWARE_URL.format(sha384=fw.sha384) for fw in gcp_firmware.FIRMWARES
        ],
    }


def _read_uki(artifact: Path, uki: Path | None) -> bytes:
    if artifact.suffix == ".efi":
        return artifact.read_bytes()
    if uki is None:
        raise MeasurementError(
            f"{artifact.name} is not a UKI and its bake recorded no UKI it boots.",
            hint="Measure the UKI target, or bake again so the disk records its boot payload.",
        )
    return uki.read_bytes()


def _gpt_event(artifact: Path) -> bytes | None:
    """The disk's GPT event digest; ``None`` for a UKI booted directly."""
    if artifact.suffix == ".efi":
        return None
    if artifact.name.endswith(".tar.gz"):
        with tarfile.open(artifact, "r:gz") as archive:
            member = next((m for m in archive if m.isfile()), None)
            stream = archive.extractfile(member) if member else None
            head = stream.read(_HEAD) if stream else b""
    elif artifact.suffix == ".gz":
        with gzip.open(artifact) as handle:
            head = handle.read(_HEAD)
    else:
        with artifact.open("rb") as handle:
            head = handle.read(_HEAD)
    return tdx.gpt_event(head)


def _placeholder(scheme: MeasurementBackend, artifact: Path, why: str) -> Measurements:
    seed = hashlib.sha256(artifact.read_bytes() if artifact.is_file() else b"").hexdigest()
    registers: dict[str, list[str | dict[str, str]]] = {}
    if scheme == "gcp":
        registers |= _gcp_firmware_registers()
    if scheme != "azure":
        for name in ("RTMR1", "RTMR2"):
            registers[name] = [hashlib.sha256(f"{scheme}:{name}:{seed}".encode()).hexdigest()]
    return placeholder_measurements(
        scheme,
        Measurements(scheme, registers, dict(REASONS[scheme]), source="placeholder"),
        context={"artifact": str(artifact), "reason": why},
    )
