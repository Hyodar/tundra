"""What the integration tests that build or boot share: the host check, the source check
(:func:`require_sources`), build removal, a reader for the GPT of a disk image (raw, a
fixed VHD, or a stream out of an archive), and what a production boot without TDX shows
(:func:`assert_fail_closed`)."""

from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest

from tundravm._modules import execution
from tundravm._source import GitSource
from tundravm.declarative._lowered import Lowered
from tundravm.testing import BootResult, boot_unavailable, build_unavailable
from tundravm.testing._boot import DEFAULT_MARKERS

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


TOKEN_SECRET = "TUNDRA_TOOLS_TOKEN"
"""The repository secret CI's template jobs read tundra-tools with."""
_PROBE_TIMEOUT = 20.0
"""Seconds :func:`require_sources` gives ``git ls-remote``."""
_NO_ACCESS = (
    "not found",
    "could not read username",
    "authentication failed",
    "permission denied",
    "error: 401",
    "error: 403",
)
"""What git says when a repository is private to this host or does not exist."""


def require_sources(image: Lowered, profiles: Sequence[str]) -> None:
    """Skip when a git source *image*'s *profiles* lock is a repository this host cannot
    read (git answers that it is not found or refuses the credentials), naming it and the
    secret; fail instead under ``TUNDRAVM_SOURCES_REQUIRED=1`` (CI's template jobs once the
    secret is set). Anything else, a dead ref, a timeout or a failed fetch, is the bake's
    to report."""
    probed: set[str] = set()
    for named in image.select(profiles).lock_sources().values():
        source = named.source
        if not isinstance(source, GitSource) or source.inline_pin is not None:
            continue
        if source.url in probed:
            continue
        probed.add(source.url)
        refused = _refused(source)
        if refused is None:
            continue
        reason = (
            f"cannot read {source.url} ({refused}); CI reads it with the {TOKEN_SECRET} "
            "repository secret"
        )
        if os.environ.get("TUNDRAVM_SOURCES_REQUIRED") == "1":
            pytest.fail(reason)
        pytest.skip(reason)


def _refused(source: GitSource) -> str | None:
    """git's error when ``git ls-remote`` finds *source*'s repository missing or refused,
    else ``None``: it answered, or failed another way."""
    try:
        done = subprocess.run(
            ["git", "ls-remote", source.url, source.ref],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode == 0:
        return None
    lines = [line.strip() for line in done.stderr.splitlines() if line.strip()]
    said = next((line for line in lines if line.startswith("fatal:")), lines[0] if lines else "")
    return said if any(words in said.lower() for words in _NO_ACCESS) else None


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


GATE = execution.GATE_UNIT
GATED = ("web.service", "greeter.service")
"""The fixture's units made from a ``Service``: each requires the execution gate."""
FAIL_CLOSED_MARKERS = (*DEFAULT_MARKERS, PROBE_DONE, execution.REFUSED)
"""What a production boot of the fixture without TDX prints: the target is reached, the
probe (a boot task, not gated) finished, and the gate refused."""
FAIL_CLOSED_UNITS = ("boot-probe", GATE, *GATED)
"""The units such a boot waits for, started or failed."""


def assert_fail_closed(result: BootResult) -> None:
    """*result* is a production boot with no TDX: it booted to its target with nothing
    fatal, ``tdx-evidence.service`` failed with the gate's line, every gated service failed
    on that dependency and never started, and the ungated probe ran."""
    summary = result.summary(tail=200)
    assert not result.timed_out and result.fatal == (), summary
    assert result.returncode in (None, 0), summary
    for marker in FAIL_CLOSED_MARKERS:
        assert marker in result.console, f"{marker!r} not on the console\n{summary}"
    assert GATE in result.failed, summary
    for unit in GATED:
        assert unit in result.failed and unit not in result.started, f"{unit}\n{summary}"
    assert f"Dependency failed for {GATED[0]}" in result.console, summary
    assert {"minimal.target", "boot-probe.service"} <= set(result.started), summary
    assert set(result.failed) == {GATE, *GATED}, summary


def free_port() -> int:
    """A TCP port on 127.0.0.1 nothing listens on, for a QEMU forward."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
