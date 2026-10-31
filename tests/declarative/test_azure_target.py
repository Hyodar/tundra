"""An ``azure`` target: the Azure glue in the payload, gated at boot, and the VHD wrapper."""

from pathlib import Path

import pytest

from tests.helpers import conf_list
from tundravm.declarative import Fragment, Recipe, Unit, Variant
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


def test_azure_unit_requires_network_setup_when_the_payload_ships_it(tmp_path: Path) -> None:
    net = Fragment("net", items=(Unit("network-setup.service", "[Service]\n", enabled=True),))
    recipe = Recipe(
        "cloud",
        items=(net,),
        variants=(Variant("default", outputs=("qemu",)), Variant("azure", outputs=("azure",))),
    )
    tree = compile_tree(recipe, path=tmp_path / "tree")
    unit = tree.unit(SERVICE, variant="azure")
    assert "After=network-setup.service network.target\nRequires=network-setup.service\n" in unit


def test_a_recipe_without_the_azure_target_carries_no_azure_glue(tmp_path: Path) -> None:
    tree = compile_tree(Recipe("plain"), path=tmp_path / "tree")
    assert "dmidecode" not in conf_list(tree.conf(), "Packages")
    assert SCRIPT not in tree.files()
    assert not (tree.root / "mkosi.images/azure").exists()
