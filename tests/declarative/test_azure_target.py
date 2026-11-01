"""An ``azure`` target: the Azure glue in the payload, gated at boot, and the VHD wrapper."""

from pathlib import Path

from tundravm.declarative import Fragment, Recipe, Unit, Variant
from tundravm.testing._model import compile_tree

SERVICE = "azure-complete-provisioning.service"


def test_azure_unit_requires_network_setup_when_the_payload_ships_it(tmp_path: Path) -> None:
    net = Fragment("net", items=(Unit("network-setup.service", "[Service]\n", enabled=True),))
    recipe = Recipe(
        "cloud",
        items=(net,),
        variants=(Variant("default", outputs=("qemu",)), Variant("azure", outputs=("azure",))),
    )
    tree = compile_tree(recipe, path=tmp_path / "tree")
    unit = tree.unit(SERVICE, profile="azure")
    assert "After=network-setup.service network.target\nRequires=network-setup.service\n" in unit
    # the provisioning script posts to the wireserver with curl
    assert "    curl\n" in tree.conf(profile="azure")
