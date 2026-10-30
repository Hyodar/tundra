"""An ``azure`` target: the Azure glue in the payload, gated at boot, and the VHD wrapper."""

from pathlib import Path

import pytest

from tests.helpers import conf_list
from tundravm._units.parse import rendered
from tundravm.compiler import AZURE_CONVERTER
from tundravm.declarative import Fragment, Recipe, Unit, Variant, compile, resolve
from tundravm.platforms.azure import AZURE_PROVISIONING_SCRIPT, provisioning_task
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


@pytest.mark.parametrize("variant", ["default", "azure"])
def test_the_payload_of_every_variant_carries_the_azure_glue(
    tree: CompiledTree, variant: str
) -> None:
    assert "dmidecode" in conf_list(tree.conf(variant=variant), "Packages")
    assert SCRIPT in tree.files(variant=variant)
    assert "ConditionVirtualization=microsoft\n" in tree.unit(SERVICE, variant=variant)


def test_azure_glue_lists_its_package_in_its_own_concern_file(tree: CompiledTree) -> None:
    glue = tree.root / "payload/mkosi.conf.d"
    (name,) = [p.name for p in glue.iterdir() if p.name.endswith("-cloud-glue.conf")]
    assert conf_list((glue / name).read_text(), "Packages") == ["dmidecode"]


def test_azure_variant_emits_provisioning_script(tree: CompiledTree) -> None:
    modes = {entry.path: entry.mode for entry in compile(RECIPE, variants=["azure"]).entries}
    assert modes[f"payload/{SCRIPT}"] == 0o755
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


def test_azure_variant_enables_service_in_postinst(tree: CompiledTree) -> None:
    postinst = tree.script("postinst", variant="azure").splitlines()

    enable_cmds = [line for line in postinst if "systemctl" in line and "enable" in line]
    assert any(SERVICE in line for line in enable_cmds)


def test_azure_variant_symlinks_to_minimal_target(tree: CompiledTree) -> None:
    postinst = tree.script("postinst", variant="azure").splitlines()

    link_cmds = [line for line in postinst if "minimal.target.wants" in line and SERVICE in line]
    assert len(link_cmds) == 1


def test_azure_variant_builds_the_vhd_wrapper(tree: CompiledTree) -> None:
    assert resolve(RECIPE, variant="azure").outputs == ("azure",)
    wrapper = tree.root / "mkosi.images/azure"
    conf = (wrapper / "mkosi.conf").read_text()
    assert "Dependencies=payload\n" in conf and "Output=azure\n" in conf
    assert "ExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI\n" in conf
    assert (wrapper / "mkosi.postoutput.d/vhd").read_text() == AZURE_CONVERTER
    assert (wrapper / "mkosi.postoutput.d/vhd").stat().st_mode & 0o111
    assert (wrapper / "mkosi.repart/00-esp.conf").read_text() == (
        tree.root / "mkosi.repart/00-esp.conf"
    ).read_text()
    # qemu-img comes from the tools tree
    assert "ToolsTreePackages=qemu-utils\n" in tree.main_conf()
    # azure only adds the wrapper: no profile overlay
    assert tree.layers("azure") == tree.layers("default")


def test_a_recipe_without_the_azure_target_carries_no_azure_glue(tmp_path: Path) -> None:
    tree = compile_tree(Recipe("plain"), path=tmp_path / "tree")
    assert "dmidecode" not in conf_list(tree.conf(), "Packages")
    assert SCRIPT not in tree.files()
    assert not (tree.root / "mkosi.images/azure").exists()
