"""Bake the ``service`` and ``cloud`` starter templates for real and check what lands in the image.

Needs mkosi >= 25 and non-interactive sudo; skipped otherwise.
Run with: uv run pytest tests/integration/test_templates_bake.py -m integration
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tundravm.backends.base import MountSpec, Requirement
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative import Repository, lower
from tundravm.declarative.lifecycle import bake_image
from tundravm.errors import BackendExecutionError
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


SNAPSHOT_ARCHIVE = "https://snapshot.debian.org/archive/{}/20251113T083151Z"
"""The template's snapshot; the test's extra repositories read from it too."""


@requires_mkosi
def test_cloud_template_bakes_with_backports_and_network_online(tmp_path: Path) -> None:
    """The cloud template bakes unpatched: snapshot, pinned backports, EFI stub, repositories."""
    recipe_path = tmp_path / "node.py"
    recipe_path.write_text(
        render_recipe_template(
            title="node",
            filename="node.py",
            base="debian/trixie",
            backend="local",
            template="cloud",
        ),
        encoding="utf-8",
    )
    recipe = load_file(recipe_path).recipe
    keyring = "/usr/share/keyrings/debian-archive-keyring.gpg"
    repositories = (
        Repository(
            "debian-updates", SNAPSHOT_ARCHIVE.format("debian"), "trixie-updates", keyring=keyring
        ),
        Repository(
            "bookworm-security",
            SNAPSHOT_ARCHIVE.format("debian-security"),
            "bookworm-security",
            keyring=keyring,
            in_image=False,
        ),
    )
    recipe = replace(
        recipe, common=replace(recipe.common, items=(*recipe.common.items, *repositories))
    )
    backend = _Capturing(LocalLinuxBackend(privilege="sudo", mkosi_args=["--format=directory"]))
    out = tmp_path / "build"
    try:
        try:
            bake_image(lower(recipe), ("default",), locked=None, backend=backend, out=out)
        except BackendExecutionError:
            print("\n".join(backend.lines[-80:]))  # pytest shows it with the failure
            raise

        tree = out / "mkosi" / "default"
        conf = (tree / "mkosi.conf").read_text(encoding="utf-8")
        assert "Snapshot=20251113T083151Z\n" in conf
        assert "Mirror=" not in conf and "SandboxTrees=" not in conf
        sandbox = tree / "mkosi.sandbox/etc/apt"
        sources = (sandbox / "sources.list.d/debian-backports.sources").read_text(encoding="utf-8")
        assert "Suites: trixie-backports\n" in sources
        assert "Pin: release n=sid\nPin-Priority: 100\n" in (
            sandbox / "preferences.d/debian-backports.pref"
        ).read_text(encoding="utf-8")
        fetched = "\n".join(backend.lines)
        for suite in ("trixie-backports", "sid", "trixie-updates", "bookworm-security"):
            assert f" {suite} InRelease" in fetched, suite

        root = out / "default" / "output" / "default"
        # The image runs trixie's kernel (6.12): sid, pinned to 100, ships a newer one.
        kernels = re.findall(r"Unpacking (linux-image-\S+) \((\S+)\)", fetched)
        assert kernels, "\n".join(line for line in backend.lines if "linux-image" in line)
        assert all(version.startswith("6.12.") for _, version in kernels), kernels
        # EfiStub installed its pinned package from the image root and cleaned up.
        assert "Preparing to unpack /systemd-boot-efi.deb" in fetched
        stub = root / "usr/lib/systemd/boot/efi/linuxx64.efi.stub"
        assert (
            subprocess.run(
                ["sudo", "grep", "-q", "LoaderInfo: systemd-stub ", str(stub)]
            ).returncode
            == 0
        )
        assert not _sudo_test("-e", root / "systemd-boot-efi.deb")
        # Only repositories declared for the image are listed in it.
        listed = root / "etc/apt/sources.list.d"
        assert "trixie-updates" in _sudo_read(listed / "debian-updates.sources")
        assert not _sudo_test("-e", listed / "bookworm-security.sources")
        assert not _sudo_test("-e", listed / "debian-backports.sources")
        unit = _sudo_read(root / "usr/lib/systemd/system/runtime-init.service")
        assert "After=network-online.target\nWants=network-online.target\n" in unit
        assert "network-setup" not in unit
        assert "Requires=runtime-init.service" in _sudo_read(
            root / "usr/lib/systemd/system/app.service"
        )
    finally:
        subprocess.run(["sudo", "rm", "-rf", str(out)], check=False)
