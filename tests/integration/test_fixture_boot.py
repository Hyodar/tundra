"""Boot the gate 1 fixture (``initramfs`` layout) both ways: ``-kernel`` and ``qemu.raw``.

``tests/fixtures/native-tree/recipe.py``'s ``recipe`` is a plain ``Recipe`` whose source
builds are a public Go program asking for a newer Go than Debian's and a Rust program
pinning a newer Rust, so it bakes where the Linux backend does with ``go``, ``rustup`` and
the network on the host. Its ``default`` variant, a
production profile, is booted under QEMU with OVMF from pflash twice: the payload's UKI
(``payload.efi``) through ``-kernel``, and ``qemu.raw`` attached as a virtio disk on a
snapshot with no ``-kernel``, which OVMF boots from the same UKI at ``EFI/BOOT/BOOTX64.EFI``
on the ESP (the way ``run --mode simulated`` boots it). There is no TDX here, so each boot
must be fail-closed (plan 6.1): ``minimal.target`` reached, ``tdx-evidence.service`` failed
with the gate's console line, the ``web`` and ``greeter`` services never started on that
dependency, and the ungated ``boot-probe`` task run, the Go and the Rust program among
what it prints.
The kernel's command line (and ``/proc/cmdline``, which the probe prints) must be the UKI's
own with the harness's SMBIOS console words appended.

CI's ``fixture-boot`` job builds it with ``tundravm build --profile default`` and points
``TUNDRAVM_FIXTURE_BUILD`` at that build directory; without it the test bakes the
``default`` variant itself. The same job builds the ``dev`` profile and runs it with
``tundravm run --mode simulated``, where the gate records ``simulated`` and the host
fetches the page. Needs a Linux build host, ``qemu-system-x86_64`` and OVMF.
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
from tundravm.testing._boot import BootPath, expected_cmdline

from .support import (
    ESP_TYPE,
    FAIL_CLOSED_MARKERS,
    FAIL_CLOSED_UNITS,
    assert_fail_closed,
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
"""A build directory ``tundravm build tests/fixtures/native-tree/recipe.py --profile default``
published (``payload.efi`` and ``qemu.raw`` at its top)."""


@pytest.fixture(scope="module")
def output(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The ``default`` variant's output directory: ``payload.efi`` and ``qemu.raw``."""
    require_boot()
    if BUILD:
        yield Path(BUILD)
        return
    build = tmp_path_factory.mktemp("fixture") / "build"
    try:
        lowered = load_file(FIXTURE / "recipe.py").lowered()
        bake_image(lowered, ("default",), locked=None, backend=LocalLinuxBackend(), build=build)
        yield build / "default" / "output"
    finally:
        remove_build(build)


def _boot(output: Path, logs: Path, via: BootPath) -> BootResult:
    """Boot until the target, the probe and the gate's refusal are on the console and the
    gate and each gated service has started or failed."""
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


def test_the_go_program_built_with_its_go_mod_toolchain_runs(kernel_boot: BootResult) -> None:
    """``git-codereview``, whose go.mod says ``go 1.26.0`` (trixie's Go is 1.24), built in the
    offline sandbox with the toolchain the host fetched, runs and prints its usage line."""
    assert probe_lines(kernel_boot.console, "go") == ["Usage: git-codereview <command> [-n] [-v]"]


def test_the_rust_program_built_with_its_pinned_toolchain_runs(kernel_boot: BootResult) -> None:
    """``rust-probe``, whose rust-toolchain.toml pins 1.91.0 (trixie's Rust is 1.85), built
    in the offline sandbox with the toolchain the host's rustup installed, says so."""
    assert probe_lines(kernel_boot.console, "rust") == ["built by rustc 1.91.0"]


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
