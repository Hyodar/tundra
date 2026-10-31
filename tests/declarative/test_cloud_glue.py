"""One UKI for every cloud: typed conditions, gated cloud glue, the cloud-glue lint."""

from pathlib import Path

import pytest

from tests.helpers import conf_list
from tundravm.declarative import (
    Condition,
    Recipe,
    Service,
    Task,
    Unit,
    Variant,
    lint,
)
from tundravm.errors import ValidationError
from tundravm.testing import compile_tree

UNITS = "mkosi.extra/usr/lib/systemd/system"
CLOUDS = Recipe("cloud", variants=(Variant("default", outputs=("qemu", "azure", "gcp")),))


def test_condition_constructors_render_their_directive() -> None:
    assert Condition.virtualization("microsoft").entry == ("ConditionVirtualization", "microsoft")
    assert Condition.virtualization("google", negate=True).value == "!google"
    assert Condition.path_exists("/dev/tdx_guest").key == "ConditionPathExists"
    assert Condition.kernel_command_line("debug").key == "ConditionKernelCommandLine"
    assert Condition.first_boot(first=False).entry == ("ConditionFirstBoot", "no")


@pytest.mark.parametrize(
    "build",
    [
        lambda: Condition.virtualization("azure"),
        lambda: Condition.path_exists("relative"),
        lambda: Condition.kernel_command_line("two words"),
        lambda: Task("t", "/bin/true", conditions=("ConditionVirtualization=google",)),  # type: ignore[arg-type]
    ],
)
def test_bad_conditions_are_refused(build: object) -> None:
    with pytest.raises(ValidationError):
        build()  # type: ignore[operator]


def test_service_and_task_render_conditions_after_the_relations(tmp_path: Path) -> None:
    gated = (Condition.virtualization("kvm"), Condition.path_exists("/dev/tdx_guest"))
    recipe = Recipe(
        "gated",
        items=(
            Service("app", "/usr/bin/app", after=("network.target",), conditions=gated),
            Task("once", "/usr/bin/once", conditions=list(gated)),  # type: ignore[arg-type]
        ),
    )
    tree = compile_tree(recipe, path=tmp_path / "tree")
    for name in ("app", "once"):
        unit = tree.read(f"{UNITS}/{name}.service").split("\n\n")[0].splitlines()
        assert unit[-2:] == ["ConditionVirtualization=kvm", "ConditionPathExists=/dev/tdx_guest"]


def test_one_variant_builds_one_uki_for_three_outputs(tmp_path: Path) -> None:
    tree = compile_tree(CLOUDS, path=tmp_path / "tree")
    conf = tree.conf(variant="default").splitlines()
    assert "Format=uki" in conf and "Output=payload" in conf
    images = tree.root / "mkosi.images"
    assert sorted(p.name for p in images.iterdir()) == ["azure", "gcp", "payload"]
    for target in ("azure", "gcp"):
        wrapper = (images / target / "mkosi.conf").read_text()
        assert "ExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI\n" in wrapper
    # the QEMU disk is the main image around the same UKI
    assert "Output=qemu\n" in tree.main_conf()
    azure = tree.read(f"{UNITS}/azure-complete-provisioning.service")
    gcp = tree.read(f"{UNITS}/gcp-metadata-dns.service")
    assert "ConditionVirtualization=microsoft" in azure
    assert "ConditionVirtualization=google" in gcp
    assert "mkosi.extra/etc/resolv.conf" not in tree.files()
    assert [d.code for d in lint(CLOUDS) if d.code == "cloud-glue-unconditional"] == []


@pytest.mark.parametrize(
    ("packages", "files", "image"),
    [
        (("dmidecode",), ("mkosi.extra/usr/bin/azure-complete-provisioning",), "azure"),
        ((), (f"{UNITS}/gcp-metadata-dns.service", "mkosi.extra/etc/hosts"), "gcp"),
    ],
    ids=["azure", "gcp"],
)
def test_a_recipe_without_cloud_targets_carries_no_glue(
    tmp_path: Path, packages: tuple[str, ...], files: tuple[str, ...], image: str
) -> None:
    tree = compile_tree(Recipe("plain"), path=tmp_path / "tree")
    for package in packages:
        assert package not in conf_list(tree.conf(), "Packages")
    for path in files:
        assert path not in tree.files()
    assert not (tree.root / f"mkosi.images/{image}").exists()


def _glue(text: str) -> Unit:
    return Unit("walinuxagent.service", content=text, enabled=False)


UNGATED = "[Unit]\nDescription=agent\n\n[Service]\nExecStart=/usr/sbin/waagent\n"
GATED = UNGATED.replace(
    "Description=agent\n", "Description=agent\nConditionVirtualization=microsoft\n"
)


@pytest.mark.parametrize(
    ("outputs", "text", "found"),
    [
        (("qemu", "azure"), UNGATED, True),
        (("qemu", "azure"), GATED, False),
        # every tree also builds the QEMU disk around the payload: the agent boots there too
        (("azure",), UNGATED, True),
    ],
)
def test_cloud_glue_unconditional(outputs: tuple[str, ...], text: str, found: bool) -> None:
    recipe = Recipe(
        "agent",
        items=(_glue(text),),
        variants=(Variant("default", outputs=outputs),),  # type: ignore[arg-type]
    )
    codes = [d for d in lint(recipe) if d.code == "cloud-glue-unconditional"]
    assert bool(codes) is found
    if found:
        assert codes[0].severity == "warning"
        assert "ConditionVirtualization=microsoft" in codes[0].message
