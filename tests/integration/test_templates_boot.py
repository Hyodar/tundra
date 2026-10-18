"""Bake the ``minimal`` and ``service`` templates and boot them under QEMU with OVMF.

The UKI boots through ``-kernel`` as ``deploy --target qemu`` boots it without TDX, on KVM
when ``/dev/kvm`` is usable and on TCG otherwise. Needs mkosi >= 25, non-interactive sudo,
``qemu-system-x86_64`` and OVMF; skipped otherwise. CI's ``boot`` job runs it.
Run with: uv run pytest tests/integration/test_templates_boot.py -m boot
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tundravm import File
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative import lower
from tundravm.declarative.lifecycle import bake_image
from tundravm.declarative.model import Declaration
from tundravm.recipe import load_file
from tundravm.templates import render_recipe_template
from tundravm.testing import BootResult, boot, boot_unavailable

pytestmark = [pytest.mark.integration, pytest.mark.boot]

BOOT_TIMEOUT = float(os.environ.get("TUNDRAVM_BOOT_TIMEOUT", "900"))
"""Seconds a boot may take: TCG on a CI runner is slow."""
LOG_DIR = os.environ.get("TUNDRAVM_BOOT_LOGS")
"""Where console logs go (CI uploads them); a pytest temp directory when unset."""

# The service template leaves shipping /usr/bin/app to the user: the test ships a stub
# that stays up, so the strict-sandboxed app.service starts and keeps running.
APP_STUB = '#!/bin/sh\necho "app stub: $*"\nexec sleep infinity\n'


def _sudo_works() -> bool:
    """Whether ``sudo`` runs without a password (``-n`` refuses some password-less setups)."""
    if shutil.which("sudo") is None:
        return False
    try:
        done = subprocess.run(
            ["sudo", "true"], stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False
        )
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0


def _require_boot() -> None:
    """Skip without mkosi, sudo, QEMU or OVMF; fail instead under ``TUNDRAVM_BOOT_REQUIRED=1``
    (CI's boot job), so a runner that lost a tool cannot pass by skipping."""
    reason = boot_unavailable()
    if shutil.which("mkosi") is None or not _sudo_works():
        reason = "needs mkosi and non-interactive sudo"
    if reason is None:
        return
    if os.environ.get("TUNDRAVM_BOOT_REQUIRED") == "1":
        pytest.fail(f"cannot boot here: {reason}")
    pytest.skip(reason)


def _bake_and_boot(
    tmp: Path, template: str, extra: tuple[Declaration, ...], expect_units: tuple[str, ...]
) -> BootResult:
    """Bake *template*'s ``default`` variant (a UKI) with *extra* items and boot it."""
    recipe_path = tmp / "node.py"
    recipe_path.write_text(
        render_recipe_template(
            title="node", filename="node.py", release="trixie", backend="local", template=template
        ),
        encoding="utf-8",
    )
    recipe = load_file(recipe_path).recipe
    recipe = replace(recipe, items=(*recipe.items, *extra))
    out = tmp / "build"
    try:
        backend = LocalLinuxBackend(privilege="sudo")
        _, artifacts = bake_image(
            lower(recipe), ("default",), locked=None, backend=backend, build=out
        )
        (artifact,) = (artifact for artifact in artifacts if artifact.target == "qemu")
        log = Path(LOG_DIR or tmp) / f"{template}.console.log"
        result = boot(
            artifact, timeout=BOOT_TIMEOUT, memory="4G", expect_units=expect_units, log=log
        )
        print(result.summary(tail=400))  # recorded in the test log
        return result
    finally:
        subprocess.run(["sudo", "rm", "-rf", str(out)], check=False)


@pytest.fixture(scope="module")
def minimal_boot(tmp_path_factory: pytest.TempPathFactory) -> BootResult:
    """The ``minimal`` template baked and booted once."""
    _require_boot()
    return _bake_and_boot(tmp_path_factory.mktemp("minimal"), "minimal", (), ("hello.service",))


@pytest.fixture(scope="module")
def service_boot(tmp_path_factory: pytest.TempPathFactory) -> BootResult:
    """The ``service`` template (TeeBase, strict sandbox, curated modules) baked and booted."""
    _require_boot()
    stub = File.text("/usr/bin/app", APP_STUB, mode=0o755)
    return _bake_and_boot(tmp_path_factory.mktemp("service"), "service", (stub,), ("app",))


def test_minimal_template_boots_to_its_target(minimal_boot: BootResult) -> None:
    """The image reaches minimal.target, runs its hello task, and no unit fails."""
    minimal_boot.assert_booted()
    assert {"minimal.target", "hello.service"} <= set(minimal_boot.started)
    assert minimal_boot.failed == ()


def test_service_template_boots_with_its_app_running(service_boot: BootResult) -> None:
    """TeeBase, debloat and the strict sandbox leave a bootable image whose app starts."""
    service_boot.assert_booted()
    assert {"minimal.target", "app.service"} <= set(service_boot.started)
    assert service_boot.failed == ()


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
