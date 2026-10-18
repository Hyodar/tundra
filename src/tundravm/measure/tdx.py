"""TDX register replay from a UKI: the event logs OVMF, systemd-stub and Linux extend.

A pure-Python port of Easy-TEE ``attest`` (``crates/measure``, ``12f1e29``) and
flashbots ``dstack-mr-gcp`` (``b16e08b``), the tools flashbots-images pins
(``flake.nix``). A register starts at zero and each event extends it:
``value = H(value || digest)``. RTMR1 and RTMR2 depend only on the image; RTMR0
and MRTD depend on the host firmware (:mod:`gcp_firmware` holds GCP's).
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable
from dataclasses import dataclass

from tundravm.errors import MeasurementError
from tundravm.measure import gcp_firmware, pe

CALLING_EFI_APP = b"Calling EFI Application from Boot Option"
EXIT_BOOT_SERVICES = b"Exit Boot Services Invocation"
EXIT_BOOT_SERVICES_SUCCESS = b"Exit Boot Services Returned with Success"
SEPARATOR = b"\0\0\0\0"
MEASURED_SECTIONS = (
    ".linux", ".osrel", ".cmdline", ".initrd", ".splash", ".dtb", ".uname", ".sbat", ".pcrkey",
)  # fmt: skip
"""Sections systemd-stub measures, in its order (attest's list)."""
RECENT_STUB = 256
"""systemd-stub measures UKI sections into RTMR2 from this version on."""


def replay(
    digests: Iterable[bytes], algorithm: str = "sha384", start: bytes | None = None
) -> bytes:
    """The register after extending *start* (zero by default) with each digest."""
    value = start if start is not None else bytes(hashlib.new(algorithm).digest_size)
    for digest in digests:
        value = hashlib.new(algorithm, value + digest).digest()
    return value


def _h(data: bytes, algorithm: str = "sha384") -> bytes:
    return hashlib.new(algorithm, data).digest()


@dataclass(frozen=True, slots=True)
class ImageHashes:
    """The image-only event digests (attest's ``DcapImageHashes``), SHA-384."""

    uki_authenticode: bytes
    kernel_authenticode: bytes
    cmdline: bytes
    initrd: bytes
    sections: tuple[tuple[str, bytes], ...]
    stub_version: int | None

    def rtmr1(self, gpt_event: bytes | None) -> bytes:
        """RTMR1: boot from a disk (its GPT event first) or from the UKI directly."""
        head = (
            [_h(CALLING_EFI_APP), _h(SEPARATOR), gpt_event, self.uki_authenticode]
            if gpt_event is not None
            else [self.uki_authenticode, _h(CALLING_EFI_APP), _h(SEPARATOR)]
        )
        tail = [self.kernel_authenticode, _h(EXIT_BOOT_SERVICES), _h(EXIT_BOOT_SERVICES_SUCCESS)]
        return replay([*head, *tail])

    def rtmr2(self) -> bytes:
        """RTMR2: the UKI sections (systemd-stub >= 256), the command line, the initrd."""
        events: list[bytes] = []
        if self.stub_version is not None and self.stub_version >= RECENT_STUB:
            for name, digest in self.sections:
                events += [_h(name.encode() + b"\0"), digest]
        return replay([*events, self.cmdline, self.initrd])


def image_hashes(uki: bytes) -> ImageHashes:
    """The SHA-384 image hashes of the UKI *uki*."""
    image = pe.parse(uki)
    linux = image.section(".linux")
    if linux is None:
        raise MeasurementError(
            "The UKI has no .linux section.", hint="Measure a unified kernel image."
        )
    return ImageHashes(
        uki_authenticode=image.authenticode("sha384"),
        kernel_authenticode=pe.parse(linux, what="the UKI's .linux").authenticode("sha384"),
        cmdline=_h(_utf16_cmdline(image.section(".cmdline") or b"")),
        initrd=_h(image.section(".initrd") or b""),
        sections=_sections(image, "sha384"),
        stub_version=image.stub_version(),
    )


def _utf16_cmdline(raw: bytes) -> bytes:
    text = raw.rstrip(b"\0").decode("utf-8", "replace")
    return text.encode("utf-16-le") + b"\0\0"


def _sections(image: pe.PeImage, algorithm: str) -> tuple[tuple[str, bytes], ...]:
    present = {s.name for s in image.sections}
    return tuple(
        (name, _h(image.section(name) or b"", algorithm))
        for name in MEASURED_SECTIONS
        if name in present
    )


def gpt_event(disk_head: bytes) -> bytes:
    """The ``EV_EFI_GPT_EVENT`` digest of a disk from its first sectors: the primary header,
    the count of used entries and those entries (UEFI_GPT_DATA)."""
    header = disk_head[512 : 512 + 92]
    if header[:8] != b"EFI PART":
        raise MeasurementError(
            "The disk image has no GPT header.", hint="Measure a GPT disk image or a UKI."
        )
    count, size = struct.unpack_from("<II", header, 80)
    table = disk_head[1024 : 1024 + count * size]
    used = [table[i : i + size] for i in range(0, len(table), size) if any(table[i : i + 16])]
    return _h(header + struct.pack("<Q", len(used)) + b"".join(used))


def vtpm_pcrs(uki: bytes) -> dict[str, str]:
    """Azure vTPM PCR4, PCR9 and PCR11 (SHA-256) of the UKI *uki* (attest ``azure.rs``)."""
    image = pe.parse(uki)
    linux = image.section(".linux") or b""
    pcr4 = replay(
        [
            _h(CALLING_EFI_APP, "sha256"),
            _h(SEPARATOR, "sha256"),
            image.authenticode("sha256"),
            pe.parse(linux, what="the UKI's .linux").authenticode("sha256"),
        ],
        "sha256",
    )
    cmdline = _utf16_cmdline(image.section(".cmdline") or b"")
    initrd = image.section(".initrd")
    pcr9 = replay(
        [_h(cmdline, "sha256"), *([] if initrd is None else [_h(initrd, "sha256")])], "sha256"
    )
    pcr11 = replay(
        [
            event
            for name, digest in _sections(image, "sha256")
            for event in (_h(name.encode() + b"\0", "sha256"), digest)
        ],
        "sha256",
    )
    return {"PCR4": pcr4.hex(), "PCR9": pcr9.hex(), "PCR11": pcr11.hex()}


def gcp_rtmr0() -> list[tuple[str, str, str]]:
    """Every RTMR0 a GCP host may report: ``(value, firmware label, machine/ACPI label)``
    per firmware x machine type x ACPI table set (dstack-mr-gcp ``MeasureRTMR0``)."""
    found = []
    variables = [bytes.fromhex(v) for v in gcp_firmware.SECURE_BOOT_VARIABLES]
    for firmware in gcp_firmware.FIRMWARES:
        for machine, (hob, acpi_sets) in sorted(gcp_firmware.MACHINE_TYPES.items()):
            for acpi_name, acpi in sorted(acpi_sets.items()):
                log = [
                    bytes.fromhex(hob),
                    bytes.fromhex(firmware.cfv),
                    *variables,
                    _h(SEPARATOR),
                    *(bytes.fromhex(v) for v in acpi),
                    _h(gcp_firmware.BOOT_ORDER),
                    bytes.fromhex(gcp_firmware.BOOT_0001),
                    bytes.fromhex(gcp_firmware.BOOT_0002),
                    bytes.fromhex(gcp_firmware.BOOT_0000),
                ]
                found.append((replay(log).hex(), firmware.label, f"{machine} acpi-{acpi_name}"))
    return found
