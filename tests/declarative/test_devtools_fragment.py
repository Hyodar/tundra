"""A variant that adds the ``DevTools()`` fragment: a profile overlay on the payload."""

from pathlib import Path

import pytest

from tundravm._units import v1
from tundravm.declarative import Debloat, Package, Recipe, Unit, Variant
from tundravm.declarative.utils import DevTools
from tundravm.testing._model import CompiledTree, compile_tree

SERIAL_UNIT = "serial-console.service"
GETTY = "serial-getty@ttyS0.service"
EXPECTED_PACKAGES = {
    "apt",
    "bash-completion",
    "curl",
    "dnsutils",
    "iputils-ping",
    "net-tools",
    "netcat-openbsd",
    "openssh-server",
    "socat",
    "strace",
    "tcpdump",
    "tcpflow",
    "vim",
}


def _recipe(*common: Debloat) -> Recipe:
    return Recipe(
        "debug",
        items=common,
        variants=(Variant("default"), Variant("devtools", add=DevTools())),
    )


@pytest.fixture
def tree(tmp_path: Path) -> CompiledTree:
    return compile_tree(_recipe(), path=tmp_path / "tree")


def test_devtools_variant_includes_expected_packages() -> None:
    """Verify the exact package list matches the upstream devtools profile."""
    declared = {item.name for item in DevTools().items if isinstance(item, Package)}
    assert declared == EXPECTED_PACKAGES


def test_devtools_enables_the_serial_getty_at_build_time(tree: CompiledTree) -> None:
    (unit,) = [item for item in DevTools().items if isinstance(item, Unit)]
    assert (unit.name, unit.content, unit.enabled) == (GETTY, None, True)
    assert f"mkosi.extra/usr/lib/systemd/system/{SERIAL_UNIT}" not in tree.files(profile="devtools")
    postinst = tree.script("postinst", profile="devtools").splitlines()
    assert f"mkosi-chroot systemctl enable {GETTY}" in postinst
    assert any("minimal.target.wants" in line and GETTY in line for line in postinst)
    # The shared payload's debloat masks the getty's template, which only devtools keeps:
    # the overlay unmasks it before it enables the getty, so the payload names no profile.
    shared = (tree.root / "payload/mkosi.postinst.d/50-postinst.sh").read_text(encoding="utf-8")
    assert '"serial-getty@.service"' not in shared and "PROFILES" not in shared
    overlay = tree.root / "payload/mkosi.profiles/devtools/mkosi.postinst.d/50-postinst.sh"
    own = overlay.read_text(encoding="utf-8").splitlines()
    unmask = own.index('for unit in "serial-getty@.service"; do')
    assert unmask < own.index(f"mkosi-chroot systemctl enable {GETTY}")


def test_unreproducible_devtools_keep_the_boot_time_serial_console(tmp_path: Path) -> None:
    recipe = Recipe("debug", variants=(Variant("devtools", add=DevTools(reproducible=False)),))
    tree = compile_tree(recipe, path=tmp_path / "tree")
    service = tree.unit(SERIAL_UNIT, profile="devtools")
    assert service == v1.SERIAL_CONSOLE
    postinst = tree.script("postinst", profile="devtools")
    enable = postinst.index(f"mkosi-chroot systemctl enable {SERIAL_UNIT}")
    assert enable < postinst.index("openssl passwd")  # the hook keeps its historical place
