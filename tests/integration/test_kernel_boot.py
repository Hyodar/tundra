"""Boot the built-kernel fixture: a kernel compiled from Debian's config with every driver in.

``tests/fixtures/kernel/recipe.py`` builds its payload's kernel from source (Debian's
``cloud-amd64`` config, ``CONFIG_MODULES=n``); the build's drivers check already asserted
every target's drivers ``=y``. This boots ``qemu.raw`` through its ESP under QEMU with OVMF
and reads what the fixture's ``boot-probe`` task printed: the release, no module file, the
drivers registered and the file systems the root layouts and volumes mount.

CI's ``kernel`` workflow builds it with ``tundravm build`` and points
``TUNDRAVM_KERNEL_BUILD`` at the build directory; the test needs one (a kernel build is too
slow to bake here). Run with: uv run pytest tests/integration/test_kernel_boot.py -m boot
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.fixtures.kernel.recipe import DRIVERS, FILESYSTEMS, VERSION
from tundravm.testing import BootResult, boot
from tundravm.testing._boot import DEFAULT_MARKERS

from .support import PROBE_DONE, probe_lines, require_boot

pytestmark = [pytest.mark.integration, pytest.mark.boot]

BOOT_TIMEOUT = float(os.environ.get("TUNDRAVM_BOOT_TIMEOUT", "900"))
LOG_DIR = os.environ.get("TUNDRAVM_BOOT_LOGS")
BUILD = os.environ.get("TUNDRAVM_KERNEL_BUILD")
"""The build directory ``tundravm build tests/fixtures/kernel/recipe.py`` published."""


@pytest.fixture(scope="module")
def booted(tmp_path_factory: pytest.TempPathFactory) -> BootResult:
    require_boot()
    if not BUILD:
        pytest.skip("TUNDRAVM_KERNEL_BUILD names no build of tests/fixtures/kernel/recipe.py")
    log = Path(LOG_DIR or tmp_path_factory.mktemp("kernel-logs")) / "kernel-disk.console.log"
    result = boot(
        Path(BUILD) / "qemu.raw",
        via="disk",
        timeout=BOOT_TIMEOUT,
        memory="4G",
        markers=(*DEFAULT_MARKERS, PROBE_DONE),
        expect_units=("boot-probe",),
        settle=5.0,
        log=log,
    )
    print(" ".join(result.command))  # recorded in the test log
    print(result.summary(tail=400))
    return result


def test_the_built_kernel_boots_with_every_driver_built_in(booted: BootResult) -> None:
    summary = booted.summary(tail=200)
    assert not booted.timed_out and booted.fatal == (), summary
    assert "boot-probe.service" in booted.started, summary
    assert probe_lines(booted.console, "release") == [VERSION]
    assert probe_lines(booted.console, "module-files") == ["0"]
    assert probe_lines(booted.console, "drivers") == [" ".join(p.rsplit("/")[-1] for p in DRIVERS)]
    assert probe_lines(booted.console, "filesystems") == [" ".join(FILESYSTEMS)]
