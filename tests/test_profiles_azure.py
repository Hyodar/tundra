"""An ``azure`` target variant gets the Azure platform integration."""

from pathlib import Path

import pytest

from tundravm.declarative import Fragment, Recipe, Variant, compile, resolve
from tundravm.platforms.azure import AZURE_PROVISIONING_SCRIPT, AZURE_PROVISIONING_SERVICE
from tundravm.testing import CompiledTree, compile_tree

SCRIPT = "mkosi.extra/usr/bin/azure-complete-provisioning"
SERVICE = "azure-complete-provisioning.service"
RECIPE = Recipe(
    "cloud",
    Fragment("common"),
    variants=(Variant("default", target="qemu"), Variant("azure", target="azure")),
)


@pytest.fixture
def tree(tmp_path: Path) -> CompiledTree:
    return compile_tree(RECIPE, path=tmp_path / "tree")


def _packages(conf: str) -> list[str]:
    lines = conf.splitlines()
    start = lines.index("Packages=") + 1 if "Packages=" in lines else len(lines)
    values: list[str] = []
    for line in lines[start:]:
        if not line.startswith("    "):
            break
        values.append(line.strip())
    return values


def test_azure_profile_adds_dmidecode_package(tree: CompiledTree) -> None:
    assert "dmidecode" in _packages(tree.conf(profile="azure"))


def test_azure_profile_emits_provisioning_script(tree: CompiledTree) -> None:
    assert SCRIPT in tree.files(profile="azure")
    modes = {entry.path: entry.mode for entry in compile(RECIPE, variants=["azure"]).entries}
    assert modes[f"azure/{SCRIPT}"] == 0o755
    assert tree.read(SCRIPT, profile="azure") == AZURE_PROVISIONING_SCRIPT


def test_azure_provisioning_script_content() -> None:
    """Verify the provisioning script checks dmidecode and contacts wireserver."""
    assert "dmidecode -s system-manufacturer" in AZURE_PROVISIONING_SCRIPT
    assert "Microsoft Corporation" in AZURE_PROVISIONING_SCRIPT
    assert "168.63.129.16" in AZURE_PROVISIONING_SCRIPT
    assert "Health" in AZURE_PROVISIONING_SCRIPT
    assert "Ready" in AZURE_PROVISIONING_SCRIPT
    assert "MAX_RETRIES=5" in AZURE_PROVISIONING_SCRIPT
    assert "goalstate" in AZURE_PROVISIONING_SCRIPT


def test_azure_profile_emits_service_unit(tree: CompiledTree) -> None:
    assert tree.unit(SERVICE, profile="azure") == AZURE_PROVISIONING_SERVICE


def test_azure_service_unit_content() -> None:
    """Verify the service unit has correct Type, After, Requires, RemainAfterExit."""
    assert "Type=oneshot" in AZURE_PROVISIONING_SERVICE
    assert "After=network.target network-setup.service" in AZURE_PROVISIONING_SERVICE
    assert "Requires=network-setup.service" in AZURE_PROVISIONING_SERVICE
    assert "RemainAfterExit=yes" in AZURE_PROVISIONING_SERVICE
    assert "ExecStart=/usr/bin/azure-complete-provisioning" in AZURE_PROVISIONING_SERVICE


def test_azure_profile_enables_service_in_postinst(tree: CompiledTree) -> None:
    postinst = tree.script("postinst", profile="azure").splitlines()

    enable_cmds = [line for line in postinst if "systemctl" in line and "enable" in line]
    assert any(SERVICE in line for line in enable_cmds)


def test_azure_profile_symlinks_to_minimal_target(tree: CompiledTree) -> None:
    postinst = tree.script("postinst", profile="azure").splitlines()

    link_cmds = [line for line in postinst if line.startswith("mkosi-chroot ln ")]
    assert len(link_cmds) == 1
    assert f"minimal.target.wants/{SERVICE}" in link_cmds[0]


def test_azure_profile_sets_output_target(tree: CompiledTree) -> None:
    assert resolve(RECIPE, variant="azure").target == "azure"
    conf = tree.conf(profile="azure").splitlines()
    assert "PostOutputScripts=scripts/azure-postoutput.sh" in conf
    assert tree.exists("scripts/azure-postoutput.sh", profile="azure")


def test_azure_profile_does_not_affect_default_profile(tree: CompiledTree) -> None:
    assert "dmidecode" not in _packages(tree.conf(profile="default"))
    assert SCRIPT not in tree.files(profile="default")
    assert not tree.exists("scripts/azure-postoutput.sh", profile="default")
