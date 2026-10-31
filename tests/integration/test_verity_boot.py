"""Boot the gate 1 fixture's verity layout from its ``qemu.raw``, through the ESP.

``tests/fixtures/native-tree/recipe.py``'s ``verity_recipe`` (``Mkosi(root="verity")``)
bakes a payload disk whose erofs root and dm-verity hash partitions the QEMU disk copies
block for block beside the ESP; the UKI on the ESP names them, with the root hash, on its
command line. Booted with no ``-kernel``, the initrd must open the root through dm-verity,
mount the erofs read-only and stack ``systemd.volatile=overlay``'s tmpfs over it; the
fixture's ``boot-probe`` task prints what the root really is and the tests read it. The
build is a production profile and there is no TDX, so the boot is fail-closed: the probe
is a boot task, which the execution gate does not hold back, and the ``web`` service never
starts (``test_fixture_boot.py`` has the gate's assertions; the dev build that serves the
page runs in the ``fixture-boot`` job).

CI's ``verity`` job builds the fixture with ``tundravm build ... --attr verity_recipe`` and
points ``TUNDRAVM_VERITY_BUILD`` at that build directory; without it the test bakes the
``default`` variant itself. Needs a Linux build host, ``qemu-system-x86_64`` and OVMF.
Run with: uv run pytest tests/integration/test_verity_boot.py -m boot
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.compiler.emit_mkosi import verity_uuids
from tundravm.declarative.lifecycle import bake_image
from tundravm.recipe import load_file
from tundravm.testing import BootResult, boot
from tundravm.testing._boot import expected_cmdline, uki_cmdline

from .support import (
    ESP_TYPE,
    FAIL_CLOSED_MARKERS,
    FAIL_CLOSED_UNITS,
    ROOT_X86_64_TYPE,
    ROOT_X86_64_VERITY_TYPE,
    assert_fail_closed,
    probe_lines,
    read_fat,
    read_gpt,
    remove_build,
    require_boot,
)

pytestmark = [pytest.mark.integration, pytest.mark.boot]

FIXTURE = Path(__file__).parents[1] / "fixtures" / "native-tree"
SEED = "6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70"
BOOT_TIMEOUT = float(os.environ.get("TUNDRAVM_BOOT_TIMEOUT", "900"))
LOG_DIR = os.environ.get("TUNDRAVM_BOOT_LOGS")
BUILD = os.environ.get("TUNDRAVM_VERITY_BUILD")
"""A build directory ``tundravm build --attr verity_recipe --profile default`` published."""


@pytest.fixture(scope="module")
def output(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The ``default`` variant's output directory: ``payload.efi``, the split partitions
    and ``qemu.raw``."""
    require_boot()
    if BUILD:
        yield Path(BUILD)
        return
    build = tmp_path_factory.mktemp("verity") / "build"
    try:
        lowered = load_file(FIXTURE / "recipe.py", attribute="verity_recipe").lowered()
        bake_image(lowered, ("default",), locked=None, backend=LocalLinuxBackend(), build=build)
        yield build / "default" / "output"
    finally:
        remove_build(build)


@pytest.fixture(scope="module")
def verity_boot(output: Path, tmp_path_factory: pytest.TempPathFactory) -> BootResult:
    """``qemu.raw`` booted through its ESP, until the probe has printed everything and the
    gate has refused."""
    log = Path(LOG_DIR or tmp_path_factory.mktemp("verity-logs")) / "verity-disk.console.log"
    result = boot(
        output / "qemu.raw",
        via="disk",
        timeout=BOOT_TIMEOUT,
        memory="4G",
        markers=FAIL_CLOSED_MARKERS,
        expect_units=FAIL_CLOSED_UNITS,
        settle=10.0,
        log=log,
    )
    print(" ".join(result.command))  # recorded in the test log
    print(result.summary(tail=400))
    return result


def test_the_ukis_cmdline_names_the_verity_root(output: Path) -> None:
    """mkosi put the root hash on the payload's UKI command line next to what the compiler
    wrote: the root device, the copied partitions by their fixed UUIDs, the volatile overlay."""
    data, hashes = verity_uuids(SEED)
    words = uki_cmdline(output / "payload.efi").split()
    assert any(
        word.startswith("roothash=") and len(word) == len("roothash=") + 64 for word in words
    )
    assert "root=/dev/mapper/root" in words
    assert f"systemd.verity_root_data=/dev/disk/by-partuuid/{data}" in words
    assert f"systemd.verity_root_hash=/dev/disk/by-partuuid/{hashes}" in words
    assert "systemd.volatile=overlay" in words


def test_qemu_disk_carries_the_payload_partitions_block_for_block(output: Path) -> None:
    """The ESP, then the root and its hash partition under the UUIDs the UKI names, each
    holding exactly the bytes of the payload's split partition."""
    data, hashes = verity_uuids(SEED)
    with (output / "qemu.raw").open("rb") as disk:
        table = read_gpt(disk)
        assert [p.type for p in table] == [ESP_TYPE, ROOT_X86_64_TYPE, ROOT_X86_64_VERITY_TYPE]
        splits = ("payload.root.raw", "payload.root-verity.raw")
        for part, split in zip(table[1:], splits, strict=True):
            assert str(part.uuid) == (data if part.type == ROOT_X86_64_TYPE else hashes)
            want = (output / split).read_bytes()
            disk.seek(part.offset)
            assert part.size >= len(want)
            got = disk.read(len(want))
            assert hashlib.sha256(got).hexdigest() == hashlib.sha256(want).hexdigest(), split


def test_qemu_disk_esp_is_a_fat32_the_firmware_reads(output: Path) -> None:
    """The ESP is FAT32 by cluster count, as UEFI firmware types a FAT."""
    with (output / "qemu.raw").open("rb") as disk:
        (esp,) = [part for part in read_gpt(disk) if part.type == ESP_TYPE]
        disk.seek(esp.offset)
        fat = read_fat(disk.read(512))
    print(f"ESP {esp.size} bytes: {fat}")
    assert fat.uefi_fat32, f"ESP {esp.size} bytes: {fat}"


def test_verity_disk_boots_through_its_esp(verity_boot: BootResult) -> None:
    """No ``-kernel``: OVMF boots the UKI off the ESP, the initrd switches to the verity
    root and the system reaches its target with the probe run, fail-closed: nothing failed
    but the gate and the services that require it."""
    assert "-kernel" not in verity_boot.command
    assert_fail_closed(verity_boot)


def test_the_kernel_boots_the_ukis_cmdline(output: Path, verity_boot: BootResult) -> None:
    """The kernel's command line, and ``/proc/cmdline`` as the probe read it, are the UKI's
    own line with the SMBIOS console words: the root hash came from the UKI, not the host."""
    expected = expected_cmdline(output / "payload.efi")
    assert verity_boot.kernel_cmdline == expected
    assert probe_lines(verity_boot.console, "cmdline") == [expected]


def test_root_is_erofs_on_dm_verity_under_a_volatile_overlay(verity_boot: BootResult) -> None:
    """The initrd opens ``root`` as a read-only dm-verity target (``CRYPT-VERITY-`` uuid),
    the kernel mounts erofs from that device on ``/sysroot``, and systemd-volatile-root
    stacks a tmpfs overlay on it: after the switch, ``/`` is that overlay (the erofs below
    it leaves the mount table with the initrd, so the kernel's own lines prove it)."""
    console = verity_boot.console
    devices = {line.split()[0]: line.split()[1:] for line in probe_lines(console, "dm")}
    assert "root" in devices, verity_boot.summary(tail=200)
    kernel_name, uuid_field, ro_field = devices["root"]
    assert uuid_field.startswith("uuid=CRYPT-VERITY-"), devices
    assert ro_field == "ro=1", devices
    for line in (
        "Finished systemd-veritysetup@root.service",
        f"erofs: (device {kernel_name}): mounted",
        "Mounted sysroot.mount",
        "Finished systemd-volatile-root.service",
    ):
        assert line in console, line
    mounts = [line.split() for line in probe_lines(console, "mount /")]
    assert [mount[:2] for mount in mounts] == [["overlay", "overlay"]], mounts
    options = mounts[0][2].split(",")
    assert "lowerdir=/sysroot" in options, options
    assert "upperdir=/run/systemd/overlay-sysroot/upper" in options, options


def test_usr_bin_writes_land_in_the_volatile_overlay(verity_boot: BootResult) -> None:
    """``systemd.volatile=overlay`` is in effect: a write under ``/usr/bin`` goes to the
    tmpfs upper layer (the verity root below it stays read-only, as above)."""
    assert probe_lines(verity_boot.console, "usr-bin") == ["write=accepted"]
