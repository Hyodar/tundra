"""What the integration tests that build or boot share: the host check, build removal, and
a reader for the GPT of a disk image (raw, a fixed VHD, or a stream out of an archive)."""

from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest

from tundravm.testing import boot_unavailable, build_unavailable

SECTOR = 512

ESP_TYPE = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
ROOT_X86_64_TYPE = uuid.UUID("4f68bce3-e8cd-4db1-96e7-fbcaf984b709")
ROOT_X86_64_VERITY_TYPE = uuid.UUID("2c7357ed-ebd2-46d9-aec1-23d437ec2bf5")
"""The Discoverable Partitions Specification's type UUIDs mkosi's ``Type=`` names resolve to."""


def require_boot() -> None:
    """Skip without a build host, QEMU or OVMF; fail instead under ``TUNDRAVM_BOOT_REQUIRED=1``
    (CI's boot jobs), so a runner that lost a tool cannot pass by skipping."""
    reason = build_unavailable() or boot_unavailable()
    if reason is None:
        return
    if os.environ.get("TUNDRAVM_BOOT_REQUIRED") == "1":
        pytest.fail(f"cannot boot here: {reason}")
    pytest.skip(reason)


def remove_build(path: Path) -> None:
    """Remove a build directory, whose image trees the backend may have written as root."""
    if shutil.which("sudo") is not None:
        subprocess.run(["sudo", "rm", "-rf", str(path)], check=False)
    else:
        shutil.rmtree(path, ignore_errors=True)


@dataclass(frozen=True, slots=True)
class GptPartition:
    """One used entry of a GPT: its type, its own UUID and its sector range."""

    type: uuid.UUID
    uuid: uuid.UUID
    first_lba: int
    last_lba: int

    @property
    def offset(self) -> int:
        return self.first_lba * SECTOR

    @property
    def size(self) -> int:
        return (self.last_lba - self.first_lba + 1) * SECTOR


def read_gpt(disk: IO[bytes]) -> tuple[GptPartition, ...]:
    """The used entries of the primary GPT of *disk* (read from its current position on:
    LBA 0 is where the stream starts), in table order."""
    head = disk.read(34 * SECTOR)
    header = head[SECTOR : 2 * SECTOR]
    assert header[:8] == b"EFI PART", "no primary GPT header at LBA 1"
    entries_lba, count, entry_size = struct.unpack_from("<QII", header, 72)
    table = head[entries_lba * SECTOR :]
    need = count * entry_size - len(table)
    if need > 0:
        table += disk.read(need)
    found = []
    for index in range(count):
        entry = table[index * entry_size : (index + 1) * entry_size]
        kind = uuid.UUID(bytes_le=entry[:16])
        if kind.int == 0:
            continue
        first, last = struct.unpack_from("<QQ", entry, 32)
        found.append(GptPartition(kind, uuid.UUID(bytes_le=entry[16:32]), first, last))
    return tuple(found)


@dataclass(frozen=True, slots=True)
class FatLayout:
    """A FAT boot sector's geometry and the cluster count that decides its type."""

    bytes_per_sector: int
    sectors_per_cluster: int
    total_sectors: int
    clusters: int
    fat32_layout: bool
    """The BPB is laid out for FAT32 (``BPB_FATSz16`` is 0)."""

    @property
    def uefi_fat32(self) -> bool:
        """A FAT32 volume firmware mounts: UEFI (and EDK II's driver) types a FAT by its
        cluster count, so a FAT32 layout with fewer than 65525 clusters is unreadable."""
        return self.fat32_layout and self.clusters >= 65525


def read_fat(boot_sector: bytes) -> FatLayout:
    """The geometry in a FAT file system's first sector (Microsoft's FAT specification)."""
    bps, spc, reserved, fats, root_entries, total16 = struct.unpack_from("<HBHBHH", boot_sector, 11)
    (fatsz16,) = struct.unpack_from("<H", boot_sector, 22)
    total32, fatsz32 = struct.unpack_from("<II", boot_sector, 32)
    fatsz = fatsz16 or fatsz32
    total = total16 or total32
    root_sectors = (root_entries * 32 + bps - 1) // bps
    data = total - (reserved + fats * fatsz + root_sectors)
    return FatLayout(bps, spc, total, data // spc, fatsz16 == 0)


PROBE = "boot-probe: "
"""What the fixture's ``BootProbe`` prefixes each console line with."""
PROBE_DONE = f"{PROBE}done"


def probe_lines(console: str, what: str) -> list[str]:
    """The probe's ``<what> ...`` lines in *console*, without the prefix and the word."""
    found = []
    for line in console.splitlines():
        _, sep, said = line.partition(PROBE)
        if sep and said.startswith(f"{what} "):
            found.append(said.removeprefix(f"{what} ").strip())
    return found


def free_port() -> int:
    """A TCP port on 127.0.0.1 nothing listens on, for a QEMU forward."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
