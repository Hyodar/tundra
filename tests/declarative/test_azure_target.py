"""An ``azure`` target variant gets the Azure platform integration."""

from pathlib import Path

import pytest

from tests.helpers import conf_list
from tundravm._units.parse import rendered
from tundravm.declarative import Fragment, Mkosi, Recipe, Unit, Variant, compile, resolve
from tundravm.platforms.azure import (
    AZURE_PROVISIONING_SCRIPT,
    AZURE_PROVISIONING_SERVICE,
    provisioning_task,
)
from tundravm.testing import CompiledTree, compile_tree

SCRIPT = "mkosi.extra/usr/bin/azure-complete-provisioning"
SERVICE = "azure-complete-provisioning.service"
RECIPE = Recipe(
    "cloud",
    variants=(Variant("default", outputs=("qemu",)), Variant("azure", outputs=("azure",))),
)


@pytest.fixture
def tree(tmp_path: Path) -> CompiledTree:
    return compile_tree(RECIPE, path=tmp_path / "tree")


def test_azure_variant_adds_dmidecode_package(tree: CompiledTree) -> None:
    assert "dmidecode" in conf_list(tree.conf(variant="azure"), "Packages")


def test_azure_variant_emits_provisioning_script(tree: CompiledTree) -> None:
    assert SCRIPT in tree.files(variant="azure")
    modes = {entry.path: entry.mode for entry in compile(RECIPE, variants=["azure"]).entries}
    assert modes[f"azure/{SCRIPT}"] == 0o755
    assert tree.read(SCRIPT, variant="azure") == AZURE_PROVISIONING_SCRIPT


def test_azure_provisioning_script_content() -> None:
    """Verify the provisioning script checks dmidecode and contacts wireserver."""
    assert "dmidecode -s system-manufacturer" in AZURE_PROVISIONING_SCRIPT
    assert "Microsoft Corporation" in AZURE_PROVISIONING_SCRIPT
    assert "168.63.129.16" in AZURE_PROVISIONING_SCRIPT
    assert "Health" in AZURE_PROVISIONING_SCRIPT
    assert "Ready" in AZURE_PROVISIONING_SCRIPT
    assert "MAX_RETRIES=5" in AZURE_PROVISIONING_SCRIPT
    assert "goalstate" in AZURE_PROVISIONING_SCRIPT


def test_azure_variant_emits_service_unit(tree: CompiledTree) -> None:
    """Without network-setup.service the unit waits for network-online.target."""
    unit = tree.unit(SERVICE, variant="azure")
    assert unit == rendered(provisioning_task(False))[SERVICE]
    assert "After=network-online.target\nWants=network-online.target\n" in unit
    assert "network-setup" not in unit
    # A typed root oneshot: no User= line, relaxed sandbox, bounded, not waiting for init.
    assert "User=" not in unit and "runtime-init" not in unit
    assert "ProtectSystem=full\n" in unit and "TimeoutStartSec=5min\n" in unit


def test_azure_unit_requires_network_setup_when_the_variant_ships_it(tmp_path: Path) -> None:
    net = Fragment("net", items=(Unit("network-setup.service", "[Service]\n", enabled=True),))
    recipe = Recipe(
        "cloud",
        items=(net,),
        variants=(Variant("default", outputs=("qemu",)), Variant("azure", outputs=("azure",))),
    )
    tree = compile_tree(recipe, path=tmp_path / "tree")
    unit = tree.unit(SERVICE, variant="azure")
    assert "After=network-setup.service network.target\nRequires=network-setup.service\n" in unit


def test_azure_unit_is_historical_under_nethermind_v1(tmp_path: Path) -> None:
    recipe = Recipe(
        "cloud",
        variants=RECIPE.variants,
        mkosi=Mkosi(dialect="nethermind-v1"),
    )
    tree = compile_tree(recipe, path=tmp_path / "tree")
    assert tree.unit(SERVICE, variant="azure") == AZURE_PROVISIONING_SERVICE


def test_azure_service_unit_content() -> None:
    """Verify the service unit has correct Type, After, Requires, RemainAfterExit."""
    assert "Type=oneshot" in AZURE_PROVISIONING_SERVICE
    assert "After=network.target network-setup.service" in AZURE_PROVISIONING_SERVICE
    assert "Requires=network-setup.service" in AZURE_PROVISIONING_SERVICE
    assert "RemainAfterExit=yes" in AZURE_PROVISIONING_SERVICE
    assert "ExecStart=/usr/bin/azure-complete-provisioning" in AZURE_PROVISIONING_SERVICE


def test_azure_variant_enables_service_in_postinst(tree: CompiledTree) -> None:
    postinst = tree.script("postinst", variant="azure").splitlines()

    enable_cmds = [line for line in postinst if "systemctl" in line and "enable" in line]
    assert any(SERVICE in line for line in enable_cmds)


def test_azure_variant_symlinks_to_minimal_target(tree: CompiledTree) -> None:
    postinst = tree.script("postinst", variant="azure").splitlines()

    link_cmds = [line for line in postinst if "minimal.target.wants" in line and SERVICE in line]
    assert len(link_cmds) == 1


def test_azure_variant_sets_output_target(tree: CompiledTree) -> None:
    assert resolve(RECIPE, variant="azure").outputs == ("azure",)
    conf = tree.conf(variant="azure").splitlines()
    assert "PostOutputScripts=scripts/azure-postoutput.sh" in conf
    assert tree.exists("scripts/azure-postoutput.sh", variant="azure")


def test_azure_variant_does_not_affect_default_variant(tree: CompiledTree) -> None:
    assert "dmidecode" not in conf_list(tree.conf(variant="default"), "Packages")
    assert SCRIPT not in tree.files(variant="default")
    assert not tree.exists("scripts/azure-postoutput.sh", variant="default")
