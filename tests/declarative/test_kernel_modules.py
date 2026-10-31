"""Curated kernel modules: ``Debian(kernel_modules=...)``, ``KernelModules=``, debloat."""

from pathlib import Path

import pytest

from tundravm.declarative import Debian, Git, Kernel, Recipe, Variant
from tundravm.declarative.model import Target
from tundravm.errors import ValidationError
from tundravm.kernel_modules import (
    TDX_GUEST_MODULES,
)
from tundravm.testing import compile_tree

RM_MODULES = 'rm -rf "$BUILDROOT/usr/lib/modules"'


def _recipe(*outputs: Target, **platform: object) -> Recipe:
    return Recipe(
        "mods",
        variants=(Variant("default", outputs=outputs or ("qemu",)),),
        platform=Debian("trixie", **platform),  # type: ignore[arg-type]
    )


def _listed(conf: str) -> list[str]:
    lines = conf.splitlines()
    if "KernelModules=" not in lines:
        return []
    start = lines.index("KernelModules=") + 1
    names = []
    for line in lines[start:]:
        if not line.startswith("    "):
            break
        names.append(line.strip())
    return names


def test_qemu_keeps_the_tdx_guest_set(tmp_path: Path) -> None:
    tree = compile_tree(_recipe(), path=tmp_path)
    assert _listed(tree.conf()) == sorted(TDX_GUEST_MODULES)
    finalize = tree.script("finalize")
    assert "for module in efivarfs nvme sd_mod tdx_guest" in finalize
    assert RM_MODULES not in finalize


def test_a_tuple_adds_to_the_curated_set(tmp_path: Path) -> None:
    tree = compile_tree(_recipe(kernel_modules=("vsock", "tun")), path=tmp_path)
    assert _listed(tree.conf()) == sorted({*TDX_GUEST_MODULES, "vsock", "tun"})


def test_all_keeps_every_module(tmp_path: Path) -> None:
    tree = compile_tree(_recipe(kernel_modules="all"), path=tmp_path)
    assert _listed(tree.conf()) == []
    finalize = tree.script("finalize")
    assert "Kernel modules" not in finalize
    assert RM_MODULES not in finalize


def test_a_built_kernel_selects_nothing_and_debloat_removes_modules(tmp_path: Path) -> None:
    recipe = Recipe(
        "built",
        items=(Kernel("6.12.1", Git("https://github.com/gregkh/linux", "v6.12.1")),),
        variants=(Variant("default"),),
        platform=Debian("trixie", kernel_package=None),
    )
    tree = compile_tree(recipe, path=tmp_path)
    assert _listed(tree.conf()) == []
    assert RM_MODULES in tree.script("finalize")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (("virtio*",), "is not a module name"),
        ("some", "must be 'all' or a tuple"),
        (("x.ko",), "is not a module name"),
    ],
)
def test_invalid_selections_are_rejected(value: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Debian("trixie", kernel_modules=value)  # type: ignore[arg-type]


def test_modules_without_a_distribution_kernel_are_rejected() -> None:
    with pytest.raises(ValidationError, match="does not install"):
        Debian("trixie", kernel_package=None, kernel_modules=("vsock",))
