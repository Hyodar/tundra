"""Curated kernel modules: ``Debian(kernel_modules=...)``, ``KernelModules=``, debloat."""

from pathlib import Path

import pytest

from tundravm.declarative import Debian, Git, Kernel, Recipe, Variant
from tundravm.declarative.model import Target
from tundravm.errors import ValidationError
from tundravm.kernel_modules import (
    TDX_GUEST_MODULES,
)
from tundravm.testing._model import compile_tree

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


BUILT_KERNEL = Recipe(
    "built",
    items=(Kernel("6.12.1", Git("https://github.com/gregkh/linux", "v6.12.1")),),
    variants=(Variant("default"),),
    platform=Debian("trixie", kernel_package=None),
)


@pytest.mark.parametrize(
    ("recipe", "listed", "in_finalize", "not_in_finalize"),
    [
        (
            _recipe(),
            sorted(TDX_GUEST_MODULES),
            ("for module in efivarfs nvme sd_mod tdx_guest",),
            (RM_MODULES,),
        ),
        (
            _recipe(kernel_modules=("vsock", "tun")),
            sorted({*TDX_GUEST_MODULES, "vsock", "tun"}),
            (),
            (),
        ),
        (_recipe(kernel_modules="all"), [], (), ("Kernel modules", RM_MODULES)),
        (BUILT_KERNEL, [], (RM_MODULES,), ()),
    ],
    ids=["qemu-default", "tuple-adds", "all", "built-kernel"],
)
def test_the_selection_lists_modules_and_debloat_follows(
    tmp_path: Path,
    recipe: Recipe,
    listed: list[str],
    in_finalize: tuple[str, ...],
    not_in_finalize: tuple[str, ...],
) -> None:
    tree = compile_tree(recipe, path=tmp_path)
    assert _listed(tree.conf()) == listed
    finalize = tree.script("finalize")
    for text in in_finalize:
        assert text in finalize
    for text in not_in_finalize:
        assert text not in finalize


@pytest.mark.parametrize(
    ("platform", "message"),
    [
        ({"kernel_modules": ("virtio*",)}, "is not a module name"),
        ({"kernel_modules": "some"}, "must be 'all' or a tuple"),
        ({"kernel_modules": ("x.ko",)}, "is not a module name"),
        ({"kernel_package": None, "kernel_modules": ("vsock",)}, "does not install"),
    ],
    ids=["glob", "word", "ko-suffix", "no-distribution-kernel"],
)
def test_invalid_selections_are_rejected(platform: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Debian("trixie", **platform)  # type: ignore[arg-type]
