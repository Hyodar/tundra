"""PE/COFF reading for UKI measurement: sections and the Authenticode digest.

A pure-Python port of what flashbots' pinned tools do with a UKI
(``foxboron/go-uefi`` ``authenticode`` in measured-boot/dstack-mr-gcp, the
``authenticode`` crate in Easy-TEE ``attest``): section data is the first
``min(VirtualSize, SizeOfRawData)`` bytes at ``PointerToRawData``.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass

from tundravm.errors import MeasurementError

_STUB_VERSION = re.compile(rb"systemd-stub (\d+)")


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    virtual_size: int
    raw_size: int
    raw_pointer: int


@dataclass(frozen=True, slots=True)
class PeImage:
    """A parsed PE file: its bytes, sections in file order and the header offsets."""

    data: bytes
    sections: tuple[Section, ...]
    checksum_offset: int
    cert_entry_offset: int | None
    size_of_headers: int

    def section(self, name: str) -> bytes | None:
        """The data of section *name*, or ``None`` when the image has none."""
        for section in self.sections:
            if section.name == name:
                size = min(section.virtual_size, section.raw_size)
                return self.data[section.raw_pointer : section.raw_pointer + size]
        return None

    def authenticode(self, algorithm: str) -> bytes:
        """The Authenticode digest: headers without checksum and certificate entry, the
        sections in file order, then trailing data short of the certificate table."""
        digest = hashlib.new(algorithm)
        data = self.data
        digest.update(data[: self.checksum_offset])
        cert_size = 0
        if self.cert_entry_offset is None:
            digest.update(data[self.checksum_offset + 4 : self.size_of_headers])
        else:
            entry = self.cert_entry_offset
            digest.update(data[self.checksum_offset + 4 : entry])
            digest.update(data[entry + 8 : self.size_of_headers])
            cert_size = struct.unpack_from("<I", data, entry + 4)[0]
        hashed = self.size_of_headers
        ordered = sorted((s for s in self.sections if s.raw_size), key=lambda s: s.raw_pointer)
        for section in ordered:
            digest.update(data[section.raw_pointer : section.raw_pointer + section.raw_size])
            hashed += section.raw_size
        extra = len(data) - hashed - cert_size
        if extra > 0:
            digest.update(data[hashed : hashed + extra])
        return digest.digest()

    def stub_version(self) -> int | None:
        """The systemd-stub major version in ``.sdmagic``; ``None`` without one."""
        magic = self.section(".sdmagic")
        match = _STUB_VERSION.search(magic or b"")
        return int(match.group(1)) if match else None


def parse(data: bytes, *, what: str = "the UKI") -> PeImage:
    """*data* as a PE image; ``MeasurementError`` when it is not one."""
    try:
        return _parse(data)
    except (struct.error, ValueError) as exc:
        raise MeasurementError(
            f"{what} is not a PE/EFI image ({exc}).",
            hint="Measure a UKI (.efi) or a disk image that boots one.",
        ) from exc


def _parse(data: bytes) -> PeImage:
    if data[:2] != b"MZ":
        raise ValueError("no MZ header")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\0\0":
        raise ValueError("no PE signature")
    count, optional_size = struct.unpack_from("<H12xH", data, pe + 6)
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic not in (0x10B, 0x20B):
        raise ValueError(f"unknown optional header magic {magic:#x}")
    directories = optional + (112 if magic == 0x20B else 96)
    rva_count = struct.unpack_from("<I", data, directories - 4)[0]
    sections = []
    for index in range(count):
        offset = optional + optional_size + 40 * index
        raw_name, virtual_size, _, raw_size, raw_pointer = struct.unpack_from(
            "<8sIIII", data, offset
        )
        name = raw_name.rstrip(b"\0").decode("ascii", "replace")
        sections.append(Section(name, virtual_size, raw_size, raw_pointer))
    return PeImage(
        data=data,
        sections=tuple(sections),
        checksum_offset=optional + 64,
        cert_entry_offset=directories + 4 * 8 if rva_count > 4 else None,
        size_of_headers=struct.unpack_from("<I", data, optional + 60)[0],
    )
