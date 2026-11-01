"""Bake stage 1 of the attested node (what ``tundravm new`` writes) and boot its dev build.

Two boot paths, on KVM when ``/dev/kvm`` is usable and on TCG otherwise: the payload's UKI
(``payload.efi``) through ``-kernel``, and the bake result's ``qemu.raw`` attached as a
virtio disk (no ``-kernel``) that OVMF boots from the same UKI on its ESP
(``EFI/BOOT/BOOTX64.EFI``), the way ``deploy --target qemu`` boots it without TDX. The
``dev`` profile is baked once, unchanged, and booted both ways; it runs simulated and passes
the boot probe its declarations compile in (``tundravm.testing.assert_probe``): the mode,
the evidence service and a refused egress datagram. Stages 2 to 6 build Nethermind from Git
with .NET, which is too slow for this job. The image's evidence service builds from this
checkout's ``tools/``. ``test_fixture_boot.py`` boots the fail-closed production profile.
Needs a host the Linux backend builds on (user namespaces or non-interactive sudo),
``qemu-system-x86_64`` and OVMF; skipped otherwise.
CI's ``boot`` job runs it.
Run with: uv run pytest tests/integration/test_stages_boot.py -m boot
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import EXAMPLE_RECIPES
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative.lifecycle import Artifact, bake_image
from tundravm.recipe import load_file
from tundravm.testing import PROBE_DONE, BootResult, assert_probe, boot
from tundravm.testing._boot import DEFAULT_MARKERS, BootPath, expected_cmdline

from .support import ESP_TYPE, read_fat, read_gpt, remove_build, require_boot

pytestmark = [pytest.mark.integration, pytest.mark.boot]

BOOT_TIMEOUT = float(os.environ.get("TUNDRAVM_BOOT_TIMEOUT", "900"))
"""Seconds a boot may take: TCG on a CI runner is slow."""
LOG_DIR = os.environ.get("TUNDRAVM_BOOT_LOGS")
"""Where console logs go (CI uploads them); a pytest temp directory when unset."""


@pytest.fixture(scope="module")
def stage1(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Artifact]:
    """Stage 1's ``dev`` profile baked once: its ``qemu`` artifact (``qemu.raw``, booting
    ``payload.efi``), removed with the build afterwards."""
    require_boot()
    tmp = tmp_path_factory.mktemp("stage1")
    lowered = load_file(EXAMPLE_RECIPES[0]).lowered()
    out = tmp / "build"
    try:
        backend = LocalLinuxBackend()
        _, artifacts = bake_image(lowered, ("dev",), locked=None, backend=backend, build=out)
        (artifact,) = (artifact for artifact in artifacts if artifact.target == "qemu")
        assert artifact.path.name == "qemu.raw", artifact.path
        uki = artifact.boot_payload
        assert uki is not None and uki.name == "payload.efi", artifact
        yield artifact
    finally:
        remove_build(out)


def _boot(artifact: Artifact, logs: Path, via: BootPath) -> BootResult:
    """Boot *artifact* through *via* until its probe finished, its console in
    ``stage1-<via>.console.log`` under ``TUNDRAVM_BOOT_LOGS`` (else *logs*)."""
    result = boot(
        artifact,
        via=via,
        timeout=BOOT_TIMEOUT,
        memory="4G",
        markers=(*DEFAULT_MARKERS, PROBE_DONE),
        log=Path(LOG_DIR or logs) / f"stage1-{via}.console.log",
    )
    print(" ".join(result.command))  # recorded in the test log
    print(result.summary(tail=400))
    return result


@pytest.fixture(scope="module")
def kernel_boot(stage1: Artifact, tmp_path_factory: pytest.TempPathFactory) -> BootResult:
    """Stage 1's UKI booted through ``-kernel``."""
    return _boot(stage1, tmp_path_factory.mktemp("stage1-logs"), "kernel")


@pytest.fixture(scope="module")
def disk_boot(stage1: Artifact, tmp_path_factory: pytest.TempPathFactory) -> BootResult:
    """Stage 1's ``qemu.raw`` booted through its ESP."""
    return _boot(stage1, tmp_path_factory.mktemp("stage1-logs"), "disk")


def _uki(artifact: Artifact) -> Path:
    assert artifact.boot_payload is not None
    return artifact.boot_payload


def test_stage1_boots_to_its_target_and_its_probe_passes(kernel_boot: BootResult) -> None:
    """The dev boot reaches minimal.target with nothing failed, and the probe derived from
    stage 1's declarations finds the mode, the evidence socket and the egress filter holding."""
    kernel_boot.assert_booted()
    assert "minimal.target" in kernel_boot.started
    assert kernel_boot.failed == () and kernel_boot.fatal == ()
    report = assert_probe(kernel_boot)
    print(report.table())
    checks = {r.check for r in report.results}
    assert "mode" in checks and any(check.startswith("egress:") for check in checks), checks


def test_stage1_boots_without_tdx(kernel_boot: BootResult) -> None:
    """tdx_guest ships in the curated module set; on a VM without TDX it stays unloaded
    and nothing fails over it."""
    assert "systemd-modules-load.service" not in kernel_boot.failed
    complaints = [
        line
        for line in kernel_boot.console.splitlines()
        if "tdx" in line.lower() and any(word in line.lower() for word in ("fail", "error"))
    ]
    assert complaints == [], complaints


def test_stage1_boots_the_ukis_cmdline(stage1: Artifact, kernel_boot: BootResult) -> None:
    """The kernel's command line (``/proc/cmdline``) is the UKI's ``.cmdline`` with the
    SMBIOS console words appended: ``-kernel`` passes no load options of its own."""
    assert kernel_boot.kernel_cmdline == expected_cmdline(_uki(stage1))


def test_stage1_esp_is_a_fat32_the_firmware_reads(stage1: Artifact) -> None:
    """``qemu.raw``'s ESP is FAT32 by cluster count (65525 or more), which is how UEFI
    firmware types a FAT: a smaller one is not mounted and the disk does not boot."""
    with stage1.path.open("rb") as disk:
        (esp,) = [part for part in read_gpt(disk) if part.type == ESP_TYPE]
        disk.seek(esp.offset)
        fat = read_fat(disk.read(512))
    print(f"ESP {esp.size} bytes: {fat}")
    assert fat.uefi_fat32, f"ESP {esp.size} bytes: {fat}"


def test_stage1_disk_boots_through_its_esp(stage1: Artifact, disk_boot: BootResult) -> None:
    """``qemu.raw`` attached as a virtio disk on a snapshot, no ``-kernel``: OVMF finds the
    UKI at ``EFI/BOOT/BOOTX64.EFI`` on the ESP, the image boots to its target with nothing
    failed, its probe passes, and systemd-stub gives the kernel the UKI's own command line."""
    command = disk_boot.command
    assert "-kernel" not in command
    assert f"file={stage1.path},format=raw,if=virtio,snapshot=on" in command
    assert any(arg.startswith("if=pflash,format=raw,readonly=on,") for arg in command)
    disk_boot.assert_booted()
    assert "minimal.target" in disk_boot.started
    assert disk_boot.failed == () and disk_boot.fatal == ()
    assert_probe(disk_boot)
    assert disk_boot.kernel_cmdline == expected_cmdline(_uki(stage1))
