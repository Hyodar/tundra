"""Bake the ``service`` and ``cloud`` starter templates for real and check what lands in the image.

Needs mkosi >= 25 and non-interactive sudo; skipped otherwise.
Run with: uv run pytest tests/integration/test_templates_bake.py -m integration
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
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


@pytest.fixture(scope="module")
def service_build(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The ``service`` template's ``default`` variant baked once, in directory format."""
    if shutil.which("mkosi") is None or not _sudo_works():
        pytest.skip("needs mkosi and non-interactive sudo")
    tmp_path = tmp_path_factory.mktemp("service")
    recipe_path = tmp_path / "node.py"
    recipe_path.write_text(
        render_recipe_template(
            title="node",
            filename="node.py",
            release="trixie",
            backend="local",
            template="service",
        ),
        encoding="utf-8",
    )
    loaded = load_file(recipe_path)
    backend = LocalLinuxBackend(privilege="sudo", mkosi_args=["--format=directory"])
    out = tmp_path / "build"
    try:
        bake_image(loaded.lowered(), ("default",), locked=None, backend=backend, build=out)
        yield out
    finally:
        subprocess.run(["sudo", "rm", "-rf", str(out)], check=False)


def test_service_template_bakes_with_its_app(service_build: Path) -> None:
    """The service template builds; its unit, state directory, config and user are in the tree."""
    root = service_build / "default" / "output" / "default"
    unit = _sudo_read(root / "usr/lib/systemd/system/app.service")
    assert "ExecStart=/usr/bin/app --config /etc/app/app.conf" in unit
    assert "User=app\n" in unit and "StateDirectory=app\n" in unit
    assert "ProtectSystem=strict\n" in unit

    wants = root / "etc/systemd/system/minimal.target.wants"
    assert _sudo_test("-L", wants / "app.service"), f"app.service is not linked into {wants}"
    assert _sudo_readlink(wants / "app.service").endswith("/app.service")
    # Debloat masks the systemd package's units but keeps the activation targets.
    for target in ("sockets.target", "timers.target"):
        assert not _sudo_test("-L", root / "etc/systemd/system" / target), target

    assert _sudo_read(root / "etc/app/app.conf") == "version=0.1.0\nlisten=0.0.0.0:8080\n"
    passwd = _sudo_read(root / "etc/passwd").splitlines()
    assert any(line.startswith("app:") and "/usr/sbin/nologin" in line for line in passwd)


def test_service_template_keeps_its_curated_kernel_modules(service_build: Path) -> None:
    """mkosi kept ``KernelModules=`` (the qemu set) and dropped the rest; debloat kept them."""
    root = service_build / "default" / "output" / "default"
    found = subprocess.run(
        ["sudo", "find", str(root / "usr/lib/modules"), "-name", "*.ko*", "-printf", "%f\\n"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    names = {name.split(".ko")[0].replace("-", "_") for name in found}
    assert {"tdx_guest", "virtio_net", "virtio_blk", "nvme"} <= names
    # Dependencies come along; drivers the qemu set does not need do not.
    assert "net_failover" in names
    assert not {"btrfs", "hv_vmbus", "gve"} & names


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
def test_service_template_units_verify_and_are_sandboxed(service_build: Path) -> None:
    """``systemd-analyze verify`` accepts every generated unit in the baked image, and the
    strict sandbox keeps ``app.service`` at exposure level OK (1.4 with systemd 255; the
    pre-step-7 unit scored 9.0 UNSAFE)."""
    root = service_build / "default" / "output" / "default"
    generated = service_build / "mkosi" / "default" / "mkosi.extra" / "usr/lib/systemd/system"
    units = sorted(p.name for p in generated.iterdir() if p.is_file())
    assert "app.service" in units
    # The template leaves shipping /usr/bin/app to the user; verify checks the command exists.
    subprocess.run(["sudo", "install", "-m", "0755", "/bin/true", str(root / "usr/bin/app")])
    verify = subprocess.run(
        ["sudo", "systemd-analyze", "verify", f"--root={root}", *units],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr
    security = subprocess.run(
        ["sudo", "systemd-analyze", "security", "--offline=yes", f"--root={root}", "app.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    found = re.search(r"Overall exposure level for app\.service: ([0-9.]+) (\w+)", security.stdout)
    assert found, security.stdout + security.stderr
    print(f"app.service exposure: {found[1]} {found[2]}")  # recorded in the test log
    assert found[2] in ("PERFECT", "SAFE", "OK"), security.stdout
    assert float(found[1]) < 5.0


NETWORK_UNITS = (
    "systemd-networkd.service",
    "systemd-networkd.socket",
    "systemd-networkd-wait-online.service",
    "nftables.service",
)
"""The packaged units TeeBase's Network and Firewall enable; debloat must leave them unmasked."""


def test_service_template_brings_up_the_network_behind_a_firewall(service_build: Path) -> None:
    """TeeBase's ``Network`` and ``Firewall``: the ``.network`` file and the ruleset survive
    debloat, their units are enabled and unmasked, and the kernel keeps nftables' modules."""
    root = service_build / "default" / "output" / "default"
    network = _sudo_read(root / "etc/systemd/network/50-tundravm.network")
    assert "Name=en* eth*\n" in network and "DHCP=yes\n" in network
    ruleset = _sudo_read(root / "etc/nftables.conf")
    assert "policy drop;" in ruleset
    assert 'tcp dport 8080 accept comment "app.service"' in ruleset
    system = root / "etc/systemd/system"
    for unit in NETWORK_UNITS:
        wants = "sockets.target.wants" if unit.endswith(".socket") else "minimal.target.wants"
        assert _sudo_test("-L", system / wants / unit), f"{unit} is not linked into {wants}"
        assert not _sudo_test("-L", system / unit), f"{unit} is masked"
    dropin = root / "usr/lib/systemd/system/systemd-networkd-wait-online.service.d"
    assert "--any --timeout=30" in _sudo_read(dropin / "50-tundravm-network.conf")
    passwd = _sudo_read(root / "etc/passwd")
    assert "\nsystemd-network:" in passwd  # networkd's User=
    found = subprocess.run(
        ["sudo", "find", str(root / "usr/lib/modules"), "-name", "nf*.ko*", "-printf", "%f\\n"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    names = {name.split(".ko")[0] for name in found}
    assert {"nf_tables", "nft_ct", "nft_reject_inet", "nf_conntrack"} <= names
    nft = shutil.which("nft") or "/usr/sbin/nft"
    if Path(nft).exists():  # the host's nft parses the ruleset against the host kernel
        check = subprocess.run(
            ["sudo", nft, "-c", "-f", str(root / "etc/nftables.conf")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert check.returncode == 0, check.stderr


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
def test_service_template_network_units_verify(service_build: Path) -> None:
    """``systemd-analyze verify`` accepts the networkd and nftables units with their drop-ins."""
    root = service_build / "default" / "output" / "default"
    verify = subprocess.run(
        ["sudo", "systemd-analyze", "verify", f"--root={root}", *NETWORK_UNITS],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr


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
            release="trixie",
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
    # A directory-format bake checks the image, not the Azure/GCP disks (they need qemu-img).
    variants = tuple(replace(variant, outputs=("qemu",)) for variant in recipe.variants)
    recipe = replace(recipe, items=(*recipe.items, *repositories), variants=variants)
    backend = _Capturing(LocalLinuxBackend(privilege="sudo", mkosi_args=["--format=directory"]))
    out = tmp_path / "build"
    try:
        try:
            bake_image(lower(recipe), ("default",), locked=None, backend=backend, build=out)
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
        # EfiStub installed its pinned package from the mounted host-fetched .deb.
        unpack = [line for line in fetched.splitlines() if "Preparing to unpack" in line]
        assert any("systemd-boot-efi" in line for line in unpack), unpack
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
        # No boot steps: the app's state directory is its StateDirectory=.
        unit = _sudo_read(root / "usr/lib/systemd/system/app.service")
        assert "StateDirectory=app\n" in unit and "runtime-init" not in unit
        assert not _sudo_test("-e", root / "usr/lib/systemd/system/runtime-init.service")
    finally:
        subprocess.run(["sudo", "rm", "-rf", str(out)], check=False)
