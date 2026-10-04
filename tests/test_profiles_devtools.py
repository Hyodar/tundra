"""A variant that adds the ``DevTools()`` fragment."""

from pathlib import Path

import pytest

from tundravm.declarative import Debloat, Fragment, Package, Recipe, Unit, Variant
from tundravm.declarative.utils import DevTools
from tundravm.testing import CompiledTree, compile_tree

SERIAL_UNIT = "serial-console.service"
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
        Fragment("common", items=common),
        variants=(Variant("default"), Variant("devtools", add=DevTools())),
    )


@pytest.fixture
def tree(tmp_path: Path) -> CompiledTree:
    return compile_tree(_recipe(), path=tmp_path / "tree")


def _packages(conf: str) -> list[str]:
    lines = conf.splitlines()
    start = lines.index("Packages=") + 1 if "Packages=" in lines else len(lines)
    values: list[str] = []
    for line in lines[start:]:
        if not line.startswith("    "):
            break
        values.append(line.strip())
    return values


def test_devtools_profile_adds_debug_packages(tree: CompiledTree) -> None:
    packages = _packages(tree.conf(profile="devtools"))
    for pkg in EXPECTED_PACKAGES:
        assert pkg in packages, f"Missing package: {pkg}"


def test_devtools_profile_includes_expected_packages() -> None:
    """Verify the exact package list matches the upstream devtools profile."""
    declared = {item.name for item in DevTools().items if isinstance(item, Package)}
    assert declared == EXPECTED_PACKAGES


def test_devtools_profile_emits_serial_console_service(tree: CompiledTree) -> None:
    (unit,) = [item for item in DevTools().items if isinstance(item, Unit)]

    assert unit.name == SERIAL_UNIT
    assert tree.unit(SERIAL_UNIT, profile="devtools") == unit.content


def test_serial_console_service_content(tree: CompiledTree) -> None:
    """Verify the serial console service enables serial-getty@ttyS0."""
    service = tree.unit(SERIAL_UNIT, profile="devtools")
    assert "serial-getty@ttyS0.service" in service
    assert "Type=oneshot" in service
    assert "RemainAfterExit=yes" in service
    assert "WantedBy=minimal.target" in service


def test_devtools_profile_enables_serial_console_in_postinst(tree: CompiledTree) -> None:
    postinst = tree.script("postinst", profile="devtools").splitlines()

    enable_cmds = [line for line in postinst if "systemctl" in line and "enable" in line]
    assert any(SERIAL_UNIT in line for line in enable_cmds)


def test_devtools_postinst_sets_root_password(tree: CompiledTree) -> None:
    """Verify the postinst script sets root password via openssl passwd."""
    postinst = tree.script("postinst", profile="devtools")
    assert 'openssl passwd -6 "tdx"' in postinst
    assert "usermod -p" in postinst
    assert "passwd -u root" in postinst


def test_devtools_postinst_configures_dropbear(tree: CompiledTree) -> None:
    """Verify the postinst script removes restrictive dropbear flags."""
    postinst = tree.script("postinst", profile="devtools")
    assert "/etc/default/dropbear" in postinst
    assert "sed -i 's/ -s//g; s/ -w//g; s/ -g//g' /etc/default/dropbear" in postinst


def test_devtools_postinst_configures_openssh(tree: CompiledTree) -> None:
    """Verify the postinst script enables password auth for openssh."""
    postinst = tree.script("postinst", profile="devtools")
    assert "PermitRootLogin yes" in postinst
    assert "PasswordAuthentication yes" in postinst


def test_devtools_profile_registers_postinst_password_hook(tree: CompiledTree) -> None:
    postinst = tree.script("postinst", profile="devtools")

    # The serial console enable line runs before the password/auth setup
    enable = postinst.index(f"mkosi-chroot systemctl enable {SERIAL_UNIT}")
    assert enable < postinst.index("openssl passwd")


def test_devtools_profile_does_not_affect_default_profile(tree: CompiledTree) -> None:
    assert "vim" not in _packages(tree.conf(profile="default"))
    assert f"mkosi.extra/usr/lib/systemd/system/{SERIAL_UNIT}" not in tree.files(profile="default")
    assert "openssl passwd" not in tree.script("postinst", profile="default")


def test_devtools_profile_bash_completion_in_debloat_skip(tmp_path: Path) -> None:
    """A path kept only for the devtools variant survives debloat there alone."""
    debloat = Debloat(keep_paths_by_variant=(("devtools", (BASH_COMPLETION,)),))
    tree = compile_tree(_recipe(debloat), path=tmp_path / "tree")

    assert "bash-completion" in _packages(tree.conf(profile="devtools"))

    finalize = tree.script("finalize", profile="default")
    assert finalize.count(f'rm -rf "$BUILDROOT{BASH_COMPLETION}"') == 1
    conditional = finalize[finalize.index("# Debloat: profile-conditional path removal") :]
    assert conditional.splitlines()[1:4] == [
        'if [[ ! "${PROFILES:-}" == *"devtools"* ]]; then',
        f'    rm -rf "$BUILDROOT{BASH_COMPLETION}"',
        "fi",
    ]
