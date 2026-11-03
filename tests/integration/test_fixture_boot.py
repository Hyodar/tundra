"""Boot the gate 1 fixture (``initramfs`` layout) both ways: ``-kernel`` and ``qemu.raw``.

``tests/fixtures/native-tree/fixture.py``'s ``image`` builds a public Go program asking for
a newer Go than Debian's and a Rust program pinning a newer Rust, so it bakes where the
Linux backend does with ``go``, ``rustup`` and the network on the host. Its ``production``
profile is booted under QEMU with OVMF from pflash twice: the payload's UKI
(``payload.efi``) through ``-kernel``, and ``qemu.raw`` attached as a virtio disk on a
snapshot with no ``-kernel``, which OVMF boots from the same UKI at ``EFI/BOOT/BOOTX64.EFI``
on the ESP (the way ``run --mode simulated`` boots it). There is no TDX here, so each boot
must be fail-closed (plan 6.1): ``minimal.target`` reached, ``tdx-evidence.service`` failed
with the gate's console line, the ``web`` and ``greeter`` services never started on that
dependency, and the instance key printed. The kernel's command line must be the UKI's own
with the harness's SMBIOS console words appended.

CI's ``fixture-boot`` job builds it with ``tundravm build`` and points
``TUNDRAVM_FIXTURE_BUILD`` at that build directory; without it the test bakes the
``production`` profile itself. The same job runs the ``dev`` profile with ``tundravm dev``,
where the gate records ``simulated``, the platform probe checks the root tmpfs and its cap
(``rootfs:initramfs``) with every service, the Go and the Rust program print their lines
and the host fetches the page. Needs a Linux build host, ``qemu-system-x86_64`` and OVMF.
Run with: uv run pytest tests/integration/test_fixture_boot.py -m boot
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative.lifecycle import bake_image
from tundravm.recipe import load_file
from tundravm.testing import BootResult, boot, read_probe
from tundravm.testing._boot import BootPath, expected_cmdline

from .support import (
    ESP_TYPE,
    FAIL_CLOSED_MARKERS,
    FAIL_CLOSED_UNITS,
    assert_fail_closed,
    read_fat,
    read_gpt,
    remove_build,
    require_boot,
)

pytestmark = [pytest.mark.integration, pytest.mark.boot]

FIXTURE = Path(__file__).parents[1] / "fixtures" / "native-tree"
BOOT_TIMEOUT = float(os.environ.get("TUNDRAVM_BOOT_TIMEOUT", "900"))
LOG_DIR = os.environ.get("TUNDRAVM_BOOT_LOGS")
BUILD = os.environ.get("TUNDRAVM_FIXTURE_BUILD")
"""A build directory ``tundravm build tests/fixtures/native-tree/fixture.py`` published
(``payload.efi`` and ``qemu.raw`` at its top)."""


@pytest.fixture(scope="module")
def output(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The ``production`` profile's output directory: ``payload.efi`` and ``qemu.raw``."""
    require_boot()
    if BUILD:
        yield Path(BUILD)
        return
    build = tmp_path_factory.mktemp("fixture") / "build"
    try:
        lowered = load_file(FIXTURE / "fixture.py").lowered()
        bake_image(lowered, ("production",), locked=None, backend=LocalLinuxBackend(), build=build)
        yield build / "production" / "output"
    finally:
        remove_build(build)


def _boot(output: Path, logs: Path, via: BootPath) -> BootResult:
    """Boot until the target, the gate's refusal and the instance key are on the console and
    the gate and each gated service has started or failed."""
    image = output / ("payload.efi" if via == "kernel" else "qemu.raw")
    log = Path(LOG_DIR or logs) / f"fixture-{via}.console.log"
    result = boot(
        image,
        via=via,
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


@pytest.fixture(scope="module")
def kernel_boot(output: Path, tmp_path_factory: pytest.TempPathFactory) -> BootResult:
    """``payload.efi`` through ``-kernel``."""
    return _boot(output, tmp_path_factory.mktemp("fixture-logs"), "kernel")


@pytest.fixture(scope="module")
def disk_boot(output: Path, tmp_path_factory: pytest.TempPathFactory) -> BootResult:
    """``qemu.raw`` through its ESP."""
    return _boot(output, tmp_path_factory.mktemp("fixture-logs"), "disk")


def test_qemu_disk_esp_is_a_fat32_the_firmware_reads(output: Path) -> None:
    """``qemu.raw``'s ESP is FAT32 by cluster count (65525 or more), which is how UEFI
    firmware types a FAT: a smaller one is not mounted and the disk does not boot."""
    with (output / "qemu.raw").open("rb") as disk:
        (esp,) = [part for part in read_gpt(disk) if part.type == ESP_TYPE]
        disk.seek(esp.offset)
        fat = read_fat(disk.read(512))
    print(f"ESP {esp.size} bytes: {fat}")
    assert fat.uefi_fat32, f"ESP {esp.size} bytes: {fat}"


def test_uki_boots_through_kernel_fail_closed(kernel_boot: BootResult) -> None:
    assert "-kernel" in kernel_boot.command
    assert_fail_closed(kernel_boot)


def test_disk_boots_through_its_esp_fail_closed(output: Path, disk_boot: BootResult) -> None:
    """No ``-kernel``: OVMF finds the UKI on the ESP of the virtio disk, and the system
    reaches its target with the gate refusing and no service started, as through
    ``-kernel``."""
    command = disk_boot.command
    assert "-kernel" not in command
    assert f"file={output / 'qemu.raw'},format=raw,if=virtio,snapshot=on" in command
    assert any(arg.startswith("if=pflash,format=raw,readonly=on,") for arg in command)
    assert_fail_closed(disk_boot)


@pytest.mark.parametrize("via", ["kernel", "disk"])
def test_the_kernel_boots_the_ukis_cmdline(
    output: Path, via: BootPath, request: pytest.FixtureRequest
) -> None:
    """The kernel's command line is the UKI's ``.cmdline`` with the SMBIOS console words:
    neither ``-kernel`` nor OVMF's boot from the ESP passes load options that would replace
    it."""
    result: BootResult = request.getfixturevalue(f"{via}_boot")
    assert result.kernel_cmdline == expected_cmdline(output / "payload.efi")


def test_the_production_build_carries_no_boot_probe(disk_boot: BootResult) -> None:
    """The derived probe ships only in a development profile: a production boot prints
    none of its lines (``tundravm.testing.read_probe``)."""
    assert read_probe(disk_boot).results == ()
