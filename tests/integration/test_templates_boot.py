"""Bake the ``minimal`` and ``service`` templates and boot them under QEMU with OVMF.

Two boot paths, on KVM when ``/dev/kvm`` is usable and on TCG otherwise: the payload's UKI
(``payload.efi``) through ``-kernel``, and the bake result's ``qemu.raw`` attached as a
virtio disk (no ``-kernel``) that OVMF boots from the same UKI on its ESP
(``EFI/BOOT/BOOTX64.EFI``), the way ``deploy --target qemu`` boots it without TDX. The
``service`` template is baked once, unchanged, and booted both ways; each time the host
fetches its page through a QEMU port forward (network, firewall and app end to end), and
each boot's kernel prints the command line the UKI carries plus the harness's console words.
The templates are ``Image`` files, and their evidence service builds tundra-tools from its
source repository, which the bake fetches. There is no TDX here, so a production profile is
fail-closed (plan 6.1: ``tdx-evidence.service`` refuses and no service starts): the
``minimal`` template's production profile is booted to show that, and the
``service`` template's ``dev`` profile, which runs simulated, serves its page.
Needs a host the Linux backend builds on (user namespaces or non-interactive sudo),
``qemu-system-x86_64``, OVMF and read access to tundra-tools (``support.require_sources``);
skipped otherwise. ``test_fixture_boot.py`` boots the same
two paths on the gate 1 fixture, which fetches no source.
CI's ``boot`` job runs it.
Run with: uv run pytest tests/integration/test_templates_boot.py -m boot
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from tundravm._modules.execution import REFUSED
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative.lifecycle import Artifact, bake_image
from tundravm.recipe import load_file
from tundravm.templates import render_recipe_template
from tundravm.testing import BootResult, boot
from tundravm.testing._boot import BootPath, expected_cmdline

from .support import (
    ESP_TYPE,
    free_port,
    read_fat,
    read_gpt,
    remove_build,
    require_boot,
    require_sources,
)

pytestmark = [pytest.mark.integration, pytest.mark.boot]

BOOT_TIMEOUT = float(os.environ.get("TUNDRAVM_BOOT_TIMEOUT", "900"))
"""Seconds a boot may take: TCG on a CI runner is slow."""
LOG_DIR = os.environ.get("TUNDRAVM_BOOT_LOGS")
"""Where console logs go (CI uploads them); a pytest temp directory when unset."""

APP_PORT = 8080
"""The port the ``service`` template's busybox httpd serves its page on."""
PAGE = "<!doctype html>\n<title>node</title>\n<h1>node 0.1.0</h1>\n"
"""What the unchanged ``service`` starter serves."""


def _bake(tmp: Path, template: str, variant: str) -> Iterator[Artifact]:
    """Bake *template*'s *variant*, unchanged: its ``qemu`` artifact (``qemu.raw``, booting
    ``payload.efi``), removed with the build afterwards."""
    recipe_path = tmp / "node.py"
    recipe_path.write_text(
        render_recipe_template(
            title="node", filename="node.py", release="trixie", backend="local", template=template
        ),
        encoding="utf-8",
    )
    lowered = load_file(recipe_path).lowered()
    require_sources(lowered, (variant,))
    out = tmp / "build"
    try:
        backend = LocalLinuxBackend()
        _, artifacts = bake_image(lowered, (variant,), locked=None, backend=backend, build=out)
        (artifact,) = (artifact for artifact in artifacts if artifact.target == "qemu")
        assert artifact.path.name == "qemu.raw", artifact.path
        uki = artifact.boot_payload
        assert uki is not None and uki.name == "payload.efi", artifact
        yield artifact
    finally:
        remove_build(out)


def _boot(
    artifact: Artifact,
    logs: Path,
    name: str,
    via: BootPath,
    expect_units: tuple[str, ...],
    forward: Mapping[int, int] | None = None,
    wait_http: Sequence[str] = (),
) -> BootResult:
    """Boot *artifact* through *via*, its console in ``<name>.console.log`` under
    ``TUNDRAVM_BOOT_LOGS`` (else *logs*)."""
    log = Path(LOG_DIR or logs) / f"{name}.console.log"
    result = boot(
        artifact,
        via=via,
        timeout=BOOT_TIMEOUT,
        memory="4G",
        expect_units=expect_units,
        log=log,
        forward=forward,
        wait_http=wait_http,
    )
    print(" ".join(result.command))  # recorded in the test log
    print(result.summary(tail=400))
    return result


@pytest.fixture(scope="module")
def minimal_artifact(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Artifact]:
    """The ``minimal`` template's production profile baked once."""
    require_boot()
    yield from _bake(tmp_path_factory.mktemp("minimal"), "minimal", "production")


@pytest.fixture(scope="module")
def minimal_boot(
    minimal_artifact: Artifact, tmp_path_factory: pytest.TempPathFactory
) -> BootResult:
    """The ``minimal`` template's UKI booted through ``-kernel``."""
    logs = tmp_path_factory.mktemp("minimal-logs")
    units = ("tdx-evidence.service", "app.service")
    return _boot(minimal_artifact, logs, "minimal", "kernel", units)


@pytest.fixture(scope="module")
def service_artifact(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Artifact]:
    """The ``service`` template (TeeBase, strict sandbox, curated modules) baked once,
    unchanged, as its ``dev`` profile (which runs without TDX), for both boot paths."""
    require_boot()
    yield from _bake(tmp_path_factory.mktemp("service"), "service", "dev")


SERVICE_URL: dict[str, str] = {}
"""The URL each ``service`` boot fetched its page from (a free host port, per boot)."""


def _service_boot(artifact: Artifact, logs: Path, via: BootPath) -> BootResult:
    host = free_port()
    SERVICE_URL[via] = url = f"http://127.0.0.1:{host}/"
    name = "service" if via == "kernel" else "service-disk"
    forward = {host: APP_PORT}
    return _boot(artifact, logs, name, via, ("app",), forward=forward, wait_http=(url,))


@pytest.fixture(scope="module")
def service_boot(
    service_artifact: Artifact, tmp_path_factory: pytest.TempPathFactory
) -> BootResult:
    """The ``service`` template's UKI booted through ``-kernel``, its page fetched."""
    return _service_boot(service_artifact, tmp_path_factory.mktemp("service-logs"), "kernel")


@pytest.fixture(scope="module")
def service_disk_boot(
    service_artifact: Artifact, tmp_path_factory: pytest.TempPathFactory
) -> BootResult:
    """The ``service`` template's ``qemu.raw`` booted through its ESP, its page fetched."""
    return _service_boot(service_artifact, tmp_path_factory.mktemp("service-logs"), "disk")


def _uki(artifact: Artifact) -> Path:
    assert artifact.boot_payload is not None
    return artifact.boot_payload


def test_minimal_template_boots_to_its_target_fail_closed(minimal_boot: BootResult) -> None:
    """The production image reaches minimal.target without TDX, its gate refuses, and its
    app never starts."""
    assert "minimal.target" in minimal_boot.started and minimal_boot.fatal == ()
    assert REFUSED in minimal_boot.console
    assert {"tdx-evidence.service", "app.service"} <= set(minimal_boot.failed)
    assert "app.service" not in minimal_boot.started


def test_minimal_template_boots_the_ukis_cmdline(
    minimal_artifact: Artifact, minimal_boot: BootResult
) -> None:
    """The kernel's command line (``/proc/cmdline``) is the UKI's ``.cmdline`` with the
    SMBIOS console words appended: ``-kernel`` passes no load options of its own."""
    assert minimal_boot.kernel_cmdline == expected_cmdline(_uki(minimal_artifact))


def test_service_template_boots_with_its_app_running(service_boot: BootResult) -> None:
    """TeeBase, debloat and the strict sandbox leave a bootable image whose app starts."""
    service_boot.assert_booted()
    assert {"minimal.target", "app.service"} <= set(service_boot.started)
    assert service_boot.failed == ()


def test_service_template_serves_its_page_to_the_host(service_boot: BootResult) -> None:
    """busybox httpd answers through QEMU's forward, so DHCP, the nftables opening for the
    app port and the sandboxed service all work: the unchanged starter is reachable."""
    assert service_boot.http_get(SERVICE_URL["kernel"]) == PAGE


def test_service_template_boots_without_tdx(service_boot: BootResult) -> None:
    """tdx_guest ships in the curated module set; on a VM without TDX it stays unloaded
    and nothing fails over it."""
    assert service_boot.fatal == ()
    assert "systemd-modules-load.service" not in service_boot.failed
    complaints = [
        line
        for line in service_boot.console.splitlines()
        if "tdx" in line.lower() and any(word in line.lower() for word in ("fail", "error"))
    ]
    assert complaints == [], complaints


def test_service_template_boots_the_ukis_cmdline(
    service_artifact: Artifact, service_boot: BootResult
) -> None:
    assert service_boot.kernel_cmdline == expected_cmdline(_uki(service_artifact))


def test_service_disk_esp_is_a_fat32_the_firmware_reads(service_artifact: Artifact) -> None:
    """``qemu.raw``'s ESP is FAT32 by cluster count (65525 or more), which is how UEFI
    firmware types a FAT: a smaller one is not mounted and the disk does not boot."""
    with service_artifact.path.open("rb") as disk:
        (esp,) = [part for part in read_gpt(disk) if part.type == ESP_TYPE]
        disk.seek(esp.offset)
        fat = read_fat(disk.read(512))
    print(f"ESP {esp.size} bytes: {fat}")
    assert fat.uefi_fat32, f"ESP {esp.size} bytes: {fat}"


def test_service_disk_boots_through_its_esp(
    service_artifact: Artifact, service_disk_boot: BootResult
) -> None:
    """``qemu.raw`` attached as a virtio disk on a snapshot, no ``-kernel``: OVMF finds the
    UKI at ``EFI/BOOT/BOOTX64.EFI`` on the ESP and the image boots to its target with the
    app running and nothing failed, as through ``-kernel``."""
    command = service_disk_boot.command
    assert "-kernel" not in command
    assert f"file={service_artifact.path},format=raw,if=virtio,snapshot=on" in command
    assert any(arg.startswith("if=pflash,format=raw,readonly=on,") for arg in command)
    service_disk_boot.assert_booted()
    assert {"minimal.target", "app.service"} <= set(service_disk_boot.started)
    assert service_disk_boot.failed == ()
    assert service_disk_boot.fatal == ()


def test_service_disk_serves_its_page_to_the_host(service_disk_boot: BootResult) -> None:
    assert service_disk_boot.http_get(SERVICE_URL["disk"]) == PAGE


def test_service_disk_boots_the_ukis_cmdline(
    service_artifact: Artifact, service_disk_boot: BootResult
) -> None:
    """Booted from the ESP, systemd-stub still gives the kernel the UKI's own command line
    (OVMF's fallback boot passes no load options that would replace it)."""
    assert service_disk_boot.kernel_cmdline == expected_cmdline(_uki(service_artifact))
