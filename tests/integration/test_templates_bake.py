"""Bake the ``service`` and ``cloud`` starter templates for real and check what lands in the image.

Needs mkosi >= 25 and non-interactive sudo; skipped otherwise.
Run with: uv run pytest tests/integration/test_templates_bake.py -m integration
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tundravm.backends.base import MountSpec, Requirement
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative.lifecycle import bake_image
from tundravm.models import BakeRequest, BakeResult
from tundravm.recipe import load_file
from tundravm.templates import render_recipe_template

pytestmark = pytest.mark.integration


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


requires_mkosi = pytest.mark.skipif(
    shutil.which("mkosi") is None or not _sudo_works(),
    reason="needs mkosi and non-interactive sudo",
)


def _sudo_read(path: Path) -> str:
    """Read a root-owned file of the built tree."""
    return subprocess.run(
        ["sudo", "cat", str(path)], capture_output=True, text=True, check=True
    ).stdout


def _sudo_test(flag: str, path: Path) -> bool:
    return subprocess.run(["sudo", "test", flag, str(path)], check=False).returncode == 0


def _sudo_readlink(path: Path) -> str:
    return subprocess.run(
        ["sudo", "readlink", str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()


@requires_mkosi
def test_service_template_bakes_with_its_app(tmp_path: Path) -> None:
    """The service template builds; its unit, init step, config and user are in the tree."""
    recipe_path = tmp_path / "node.py"
    recipe_path.write_text(
        render_recipe_template(
            title="node",
            filename="node.py",
            base="debian/trixie",
            backend="local",
            template="service",
        ),
        encoding="utf-8",
    )
    loaded = load_file(recipe_path)
    backend = LocalLinuxBackend(privilege="sudo", mkosi_args=["--format=directory"])
    out = tmp_path / "build"
    try:
        bake_image(loaded.lowered(), ("default",), locked=None, backend=backend, out=out)

        root = out / "default" / "output" / "default"
        unit = _sudo_read(root / "usr/lib/systemd/system/app.service")
        assert "ExecStart=/usr/bin/app --config /etc/app/app.conf" in unit
        assert "User=app" in unit
        assert "Requires=runtime-init.service" in unit

        wants = root / "etc/systemd/system/minimal.target.wants"
        for name in ("app.service", "runtime-init.service"):
            assert _sudo_test("-L", wants / name), f"{name} is not linked into {wants}"
            assert _sudo_readlink(wants / name).endswith(f"/{name}")

        init = root / "usr/bin/runtime-init"
        assert _sudo_test("-x", init)
        assert "mkdir -p /var/lib/app && chown app:app /var/lib/app" in _sudo_read(init)

        assert _sudo_read(root / "etc/app/app.conf") == "version=0.1.0\nlisten=0.0.0.0:8080\n"
        passwd = _sudo_read(root / "etc/passwd").splitlines()
        assert any(line.startswith("app:") for line in passwd)
    finally:
        subprocess.run(["sudo", "rm", "-rf", str(out)], check=False)


class _Capturing:
    """A :class:`LocalLinuxBackend` that keeps mkosi's output lines."""

    def __init__(self, inner: LocalLinuxBackend) -> None:
        self.inner = inner
        self.name = inner.name
        self.lines: list[str] = []

    def requirements(self) -> tuple[Requirement, ...]:
        return self.inner.requirements()

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        return self.inner.mount_plan(request)

    def prepare(self, request: BakeRequest) -> None:
        self.inner.prepare(request)

    def execute(self, request: BakeRequest) -> BakeResult:
        return self.inner.execute(replace(request, on_output=self.lines.append))

    def cleanup(self, request: BakeRequest) -> None:
        self.inner.cleanup(request)


TEMPLATE_FIXES = (
    ("    mirror=SNAPSHOT,\n    tools_mirror=SNAPSHOT,\n", ""),
    ("            EfiStub(snapshot=SNAPSHOT, version=EFI_STUB_VERSION),\n", ""),
)
"""Edits the cloud template needs to build on mkosi 26: ``Mirror=`` gets ``debian``
appended, so the full ``snapshot.debian.org/archive/debian/<ts>/`` URL does not
resolve; the 20251113 snapshot no longer carries the pinned EFI stub, and mkosi-chroot
mounts its own ``/tmp`` over the one ``EfiStub`` copies the package to."""


@requires_mkosi
def test_cloud_template_bakes_with_backports_and_network_online(tmp_path: Path) -> None:
    """Backports reach apt through mkosi.sandbox; runtime-init waits for network-online."""
    source = render_recipe_template(
        title="node", filename="node.py", base="debian/trixie", backend="local", template="cloud"
    )
    for before, after in TEMPLATE_FIXES:
        assert before in source
        source = source.replace(before, after)
    recipe_path = tmp_path / "node.py"
    recipe_path.write_text(source, encoding="utf-8")
    loaded = load_file(recipe_path)
    backend = _Capturing(LocalLinuxBackend(privilege="sudo", mkosi_args=["--format=directory"]))
    out = tmp_path / "build"
    try:
        bake_image(loaded.lowered(), ("default",), locked=None, backend=backend, out=out)

        tree = out / "mkosi" / "default"
        sources = tree / "mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources"
        assert "Suites: trixie-backports\n" in sources.read_text(encoding="utf-8")
        assert "SandboxTrees=" not in (tree / "mkosi.conf").read_text(encoding="utf-8")
        fetched = "\n".join(backend.lines)
        assert "trixie-backports InRelease" in fetched
        assert " sid InRelease" in fetched

        root = out / "default" / "output" / "default"
        # The sources configure apt during the build only.
        assert not _sudo_test("-e", root / "etc/apt/sources.list.d/debian-backports.sources")
        unit = _sudo_read(root / "usr/lib/systemd/system/runtime-init.service")
        assert "After=network-online.target\nWants=network-online.target\n" in unit
        assert "network-setup" not in unit
        assert "Requires=runtime-init.service" in _sudo_read(
            root / "usr/lib/systemd/system/app.service"
        )
    finally:
        subprocess.run(["sudo", "rm", "-rf", str(out)], check=False)
