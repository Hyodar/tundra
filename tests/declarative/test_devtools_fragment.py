"""A variant that adds the ``DevTools()`` fragment."""

from pathlib import Path

import pytest

from tests.helpers import conf_list
from tundravm._units import v1
from tundravm.declarative import Debloat, Package, Recipe, Unit, Variant
from tundravm.declarative.utils import DevTools
from tundravm.testing import CompiledTree, compile_tree

SERIAL_UNIT = "serial-console.service"
GETTY = "serial-getty@ttyS0.service"
BASH_COMPLETION = "/usr/share/bash-completion"
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


def test_devtools_variant_adds_debug_packages(tree: CompiledTree) -> None:
    packages = conf_list(tree.conf(variant="devtools"), "Packages")
    for pkg in EXPECTED_PACKAGES:
        assert pkg in packages, f"Missing package: {pkg}"


def test_devtools_variant_includes_expected_packages() -> None:
    """Verify the exact package list matches the upstream devtools profile."""
    declared = {item.name for item in DevTools().items if isinstance(item, Package)}
    assert declared == EXPECTED_PACKAGES


def test_devtools_enables_the_serial_getty_at_build_time(tree: CompiledTree) -> None:
    (unit,) = [item for item in DevTools().items if isinstance(item, Unit)]
    assert (unit.name, unit.content, unit.enabled) == (GETTY, None, True)
    assert f"mkosi.extra/usr/lib/systemd/system/{SERIAL_UNIT}" not in tree.files(variant="devtools")
    postinst = tree.script("postinst", variant="devtools").splitlines()
    assert f"mkosi-chroot systemctl enable {GETTY}" in postinst
    assert any("minimal.target.wants" in line and GETTY in line for line in postinst)
    # Debloat masks systemd's units outside its keep list: the getty's template stays.
    (keep,) = [line for line in postinst if line.startswith("systemd_svc_whitelist=")]
    assert '"serial-getty@.service"' in keep


def test_unreproducible_devtools_keep_the_boot_time_serial_console(tmp_path: Path) -> None:
    recipe = Recipe("debug", variants=(Variant("devtools", add=DevTools(reproducible=False)),))
    tree = compile_tree(recipe, path=tmp_path / "tree")
    service = tree.unit(SERIAL_UNIT, variant="devtools")
    assert service == v1.SERIAL_CONSOLE
    postinst = tree.script("postinst", variant="devtools")
    enable = postinst.index(f"mkosi-chroot systemctl enable {SERIAL_UNIT}")
    assert enable < postinst.index("openssl passwd")  # the hook keeps its historical place


def test_devtools_postinst_sets_root_password(tree: CompiledTree) -> None:
    """Verify the postinst script sets root password via openssl passwd."""
    postinst = tree.script("postinst", variant="devtools")
    assert 'openssl passwd -6 -salt "$SALT" "tdx"' in postinst
    assert "usermod -p" in postinst
    assert "passwd -u root" in postinst


def test_devtools_postinst_configures_dropbear(tree: CompiledTree) -> None:
    """Verify the postinst script removes restrictive dropbear flags."""
    postinst = tree.script("postinst", variant="devtools")
    assert "/etc/default/dropbear" in postinst
    assert "sed -i 's/ -s//g; s/ -w//g; s/ -g//g' /etc/default/dropbear" in postinst


def test_devtools_postinst_configures_openssh(tree: CompiledTree) -> None:
    """Verify the postinst script enables password auth for openssh."""
    postinst = tree.script("postinst", variant="devtools")
    assert "PermitRootLogin yes" in postinst
    assert "PasswordAuthentication yes" in postinst


def test_devtools_variant_registers_postinst_password_hook(tree: CompiledTree) -> None:
    assert "openssl passwd" in tree.script("postinst", variant="devtools")


def test_devtools_variant_does_not_affect_default_variant(tree: CompiledTree) -> None:
    assert "vim" not in conf_list(tree.conf(variant="default"), "Packages")
    assert f"mkosi.extra/usr/lib/systemd/system/{SERIAL_UNIT}" not in tree.files(variant="default")
    assert "openssl passwd" not in tree.script("postinst", variant="default")


def test_devtools_variant_bash_completion_in_debloat_skip(tmp_path: Path) -> None:
    """A path kept only for the devtools variant survives debloat there alone."""
    debloat = Debloat(keep_paths_by_variant={"devtools": (BASH_COMPLETION,)})
    tree = compile_tree(_recipe(debloat), path=tmp_path / "tree")

    assert "bash-completion" in conf_list(tree.conf(variant="devtools"), "Packages")

    finalize = tree.script("finalize", variant="default")
    assert finalize.count(f'rm -rf "$BUILDROOT{BASH_COMPLETION}"') == 1
    conditional = finalize[finalize.index("# Debloat: profile-conditional path removal") :]
    assert conditional.splitlines()[1:4] == [
        'if [[ ! "${PROFILES:-}" == *"devtools"* ]]; then',
        f'    rm -rf "$BUILDROOT{BASH_COMPLETION}"',
        "fi",
    ]
