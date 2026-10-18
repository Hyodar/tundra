"""The offline register replay against published test vectors.

Vectors: flashbots/measured-boot v1.2.0 ``measure/pcr0{4,9}_test.go`` and
``pcr11_test.go`` (SHA-256 event replay), and a real GCP TDX quote printed in
flashbots-images' ``modules/flashbox/flashbox-l1/readme.md`` (MRTD and RTMR0 of a
c3-standard-4 on firmware f53fdf89…).
"""

from __future__ import annotations

import hashlib
import struct

import pytest

from tundravm.errors import MeasurementError
from tundravm.measure import gcp_firmware, pe, tdx

GCP_QUOTE_MRTD = "feb7486608382c1ff0e15b4648ddc0acea6ca974eb53e3529f4c4bd5ffbaa20bf335cb75965cea65fe473aed9647c162"  # noqa: E501
GCP_QUOTE_RTMR0 = "e1d0235496f93f9475bf0b26d33da5c15831cfc94104d6bea7ab82db027c5f1e917d47dda6953eefae7dcb20ab6f75c4"  # noqa: E501


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def test_measured_boot_pcr4_vector() -> None:
    events = [_sha256(tdx.CALLING_EFI_APP), _sha256(tdx.SEPARATOR), bytes(32), bytes([1]) * 32]
    assert tdx.replay(events, "sha256").hex() == (
        "22116dee861aa6b44242ac469eab24cead344d52c77131f54ac1cac9d6a2408e"
    )


def test_measured_boot_pcr9_vector() -> None:
    cmdline = tdx._utf16_cmdline(b"console=tty0\0")
    assert tdx.replay([_sha256(cmdline), bytes(32)], "sha256").hex() == (
        "eb4f7bca865807d3163b95174d6e66cfc74acf8b930a553e95ec94662cb6facd"
    )


def test_measured_boot_pcr11_vector() -> None:
    events = [_sha256(b".linux\0"), bytes(32), _sha256(b".initrd\0"), bytes([1]) * 32]
    assert tdx.replay(events, "sha256").hex() == (
        "9dfe399fcd4432639f0e20f49df823aa66b095f0664f0a4b9fbdc11ea64683e2"
    )


def test_a_real_gcp_quote_matches_one_firmware_set() -> None:
    firmware = next(fw for fw in gcp_firmware.FIRMWARES if fw.mrtd == GCP_QUOTE_MRTD)
    hits = [row for row in tdx.gcp_rtmr0() if row[0] == GCP_QUOTE_RTMR0]
    assert hits == [(GCP_QUOTE_RTMR0, firmware.label, "c3-standard-4 acpi-2026-04")]


def test_the_firmware_table_is_consistent() -> None:
    values = [row[0] for row in tdx.gcp_rtmr0()]
    sets = sum(len(acpi) for _, acpi in gcp_firmware.MACHINE_TYPES.values())
    assert len(values) == len(set(values)) == len(gcp_firmware.FIRMWARES) * sets == 27
    assert len({fw.label for fw in gcp_firmware.FIRMWARES}) == len(gcp_firmware.FIRMWARES)


def _pe(sections: dict[str, bytes], *, cert: bytes = b"", checksum: int = 0) -> bytes:
    """A minimal PE32+ image: headers in 0x200 bytes, each section 0x200-aligned."""
    count = len(sections)
    head = bytearray(0x200)
    head[:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, 0x40)
    head[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<HH12xHH", head, 0x44, 0x8664, count, 240, 0x22)
    optional = 0x58
    struct.pack_into("<H", head, optional, 0x20B)
    struct.pack_into("<I", head, optional + 60, 0x200)
    struct.pack_into("<I", head, optional + 64, checksum)
    struct.pack_into("<I", head, optional + 108, 16)
    body = bytearray()
    for index, (name, data) in enumerate(sections.items()):
        raw = data + bytes(-len(data) % 0x200)
        entry = optional + 240 + 40 * index
        struct.pack_into(
            "<8sIIII", head, entry, name.encode(), len(data), 0, len(raw), 0x200 + len(body)
        )
        body += raw
    image = bytes(head) + bytes(body)
    if cert:
        struct.pack_into("<II", head, optional + 112 + 32, len(image), len(cert))
        image = bytes(head) + bytes(body) + cert
    return image


def test_authenticode_skips_the_checksum_and_the_certificate_table() -> None:
    sections = {".text": b"\x90" * 40, ".data": b"payload"}
    plain = pe.parse(_pe(sections)).authenticode("sha384")
    assert pe.parse(_pe(sections, checksum=0xDEAD)).authenticode("sha384") == plain
    assert pe.parse(_pe(sections, cert=b"\x01" * 16)).authenticode("sha384") == plain
    assert pe.parse(_pe({**sections, ".data": b"other"})).authenticode("sha384") != plain


def test_uki_sections_feed_rtmr2_only_for_a_recent_stub() -> None:
    kernel = _pe({".text": b"kernel"})
    base = {".linux": kernel, ".cmdline": b"console=ttyS0\0", ".initrd": b"initrd"}
    old = tdx.image_hashes(_pe({**base, ".sdmagic": b"#### systemd-stub 255 ####"}))
    new = tdx.image_hashes(_pe({**base, ".sdmagic": b"#### systemd-stub 257 ####"}))
    assert old.rtmr2() == tdx.replay([old.cmdline, old.initrd])
    assert new.rtmr2() != old.rtmr2() and new.stub_version == 257
    assert [name for name, _ in new.sections] == [".linux", ".cmdline", ".initrd"]
    assert new.rtmr1(None) != new.rtmr1(bytes(48))


def test_gpt_event_hashes_the_header_and_used_entries() -> None:
    head = bytearray(34 * 512)
    head[512:520] = b"EFI PART"
    struct.pack_into("<II", head, 512 + 80, 128, 128)
    head[1024:1040] = b"\x11" * 16
    expected = hashlib.sha384(
        bytes(head[512:604]) + struct.pack("<Q", 1) + bytes(head[1024:1152])
    ).digest()
    assert tdx.gpt_event(bytes(head)) == expected
    with pytest.raises(MeasurementError, match="no GPT header"):
        tdx.gpt_event(bytes(34 * 512))


def test_a_non_pe_artifact_is_a_measurement_error() -> None:
    with pytest.raises(MeasurementError, match="not a PE/EFI image"):
        pe.parse(b"not a pe file")
