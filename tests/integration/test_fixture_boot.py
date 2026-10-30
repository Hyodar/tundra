"""Boot the gate 1 fixture (``initramfs`` layout) both ways: ``-kernel`` and ``qemu.raw``.

``tests/fixtures/native-tree/recipe.py``'s ``recipe`` is a plain ``Recipe`` with no source
builds, so it bakes anywhere the Linux backend does. Its ``default`` variant is booted under
QEMU with OVMF from pflash twice: the payload's UKI (``payload.efi``) through ``-kernel``,
and ``qemu.raw`` attached as a virtio disk on a snapshot with no ``-kernel``, which OVMF
boots from the same UKI at ``EFI/BOOT/BOOTX64.EFI`` on the ESP (the way ``deploy --target
qemu`` boots it without TDX). Each boot must reach ``minimal.target`` with the fixture's
``web`` service running and its page fetched by the host through a QEMU forward, and the
kernel's command line (and ``/proc/cmdline``, which the fixture's ``boot-probe`` task prints)
must be the UKI's own with the harness's SMBIOS console words appended.

CI's ``fixture-boot`` job bakes it with ``tundravm bake`` and points
``TUNDRAVM_FIXTURE_BUILD`` at that build directory; without it the test bakes the
``default`` variant itself. Needs a Linux build host, ``qemu-system-x86_64`` and OVMF.
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
from tundravm.testing import BootResult, boot
from tundravm.testing._boot import DEFAULT_MARKERS, BootPath, expected_cmdline

from .support import (
    ESP_TYPE,
    PROBE_DONE,
    free_port,
    probe_lines,
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
"""A build directory ``tundravm bake tests/fixtures/native-tree/recipe.py --variant default``
wrote."""

WEB_PORT = 8080
PAGE = "<!doctype html>\n<title>fixture</title>\n<h1>fixture</h1>\n"
"""What the fixture's ``web`` service serves (``recipe.py``'s ``PAGE``)."""
URL: dict[str, str] = {}
"""The URL each boot fetched the page from (a free host port, per boot)."""


@pytest.fixture(scope="module")
def output(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The ``default`` variant's output directory: ``payload.efi`` and ``qemu.raw``."""
    require_boot()
    if BUILD:
        yield Path(BUILD) / "default" / "output"
        return
    build = tmp_path_factory.mktemp("fixture") / "build"
    try:
        lowered = load_file(FIXTURE / "recipe.py").lowered()
        bake_image(lowered, ("default",), locked=None, backend=LocalLinuxBackend(), build=build)
        yield build / "default" / "output"
    finally:
        remove_build(build)


def _boot(output: Path, logs: Path, via: BootPath) -> BootResult:
    host = free_port()
    URL[via] = url = f"http://127.0.0.1:{host}/"
    image = output / ("payload.efi" if via == "kernel" else "qemu.raw")
    log = Path(LOG_DIR or logs) / f"fixture-{via}.console.log"
    result = boot(
        image,
        via=via,
        timeout=BOOT_TIMEOUT,
        memory="4G",
        markers=(*DEFAULT_MARKERS, PROBE_DONE),
        expect_units=("web", "boot-probe"),
        log=log,
        forward={host: WEB_PORT},
        wait_http=(url,),
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


def _assert_booted(result: BootResult, via: BootPath) -> None:
    result.assert_booted()
    assert {"minimal.target", "web.service", "boot-probe.service"} <= set(result.started)
    assert result.failed == ()
    assert result.fatal == ()
    assert result.http_get(URL[via]) == PAGE


def test_uki_boots_through_kernel_with_its_service_serving(kernel_boot: BootResult) -> None:
    assert "-kernel" in kernel_boot.command
    _assert_booted(kernel_boot, "kernel")


def test_disk_boots_through_its_esp_with_its_service_serving(
    output: Path, disk_boot: BootResult
) -> None:
    """No ``-kernel``: OVMF finds the UKI on the ESP of the virtio disk, and the system
    reaches its target with the service up and its page served, as through ``-kernel``."""
    command = disk_boot.command
    assert "-kernel" not in command
    assert f"file={output / 'qemu.raw'},format=raw,if=virtio,snapshot=on" in command
    assert any(arg.startswith("if=pflash,format=raw,readonly=on,") for arg in command)
    _assert_booted(disk_boot, "disk")


@pytest.mark.parametrize("via", ["kernel", "disk"])
def test_the_kernel_boots_the_ukis_cmdline(
    output: Path, via: BootPath, request: pytest.FixtureRequest
) -> None:
    """The kernel's command line, and ``/proc/cmdline`` as the probe read it, are the UKI's
    ``.cmdline`` with the SMBIOS console words: neither ``-kernel`` nor OVMF's boot from the
    ESP passes load options that would replace it."""
    result: BootResult = request.getfixturevalue(f"{via}_boot")
    expected = expected_cmdline(output / "payload.efi")
    assert result.kernel_cmdline == expected
    assert probe_lines(result.console, "cmdline") == [expected]
