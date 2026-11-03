"""What components, the root and the targets add to a built kernel, and in what order."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm import DebianConfig, Image, Kernel, KernelFragment, KernelPatch, Profile, RootFS
from tundravm._image._lower import CONTAINER_KERNEL, VERITY_KERNEL, kernel_of
from tundravm.components import Component, Container
from tundravm.errors import ValidationError

BASE = DebianConfig("6.12.113-1")


class _Xfs(Component):
    def compose(self) -> tuple[()]:
        return ()

    def kernel(self) -> list[KernelFragment]:
        return [KernelFragment({"CONFIG_XFS_FS": "y"}, reason="the data disk is XFS")]


class _Node(Component):
    patch: Path

    def compose(self) -> tuple[_Xfs]:
        return (_Xfs(),)

    def kernel(self) -> list[KernelFragment | KernelPatch]:
        return [KernelFragment({"CONFIG_TLS": "y"}), KernelPatch(self.patch)]


def test_needs_merge_in_declaration_order(tmp_path: Path) -> None:
    own, node = tmp_path / "0001-own.patch", tmp_path / "0002-node.patch"
    image = Image(
        "k",
        kernel=Kernel.build(
            base=BASE, fragments=[KernelFragment({"CONFIG_BPF_SYSCALL": "y"})], patches=[own]
        ),
        components=[_Node(patch=node)],
        rootfs=RootFS.verity(),
        targets=["qemu", "gcp"],
    )
    lowered = kernel_of(image)
    assert [option for fragment in lowered.fragments for option in fragment.options] == [
        "CONFIG_BPF_SYSCALL",
        "CONFIG_TLS",
        "CONFIG_XFS_FS",
        *VERITY_KERNEL.options,
    ]
    assert [patch.name for patch in lowered.patches] == [
        "0001-own.patch",
        "0002-node.patch",
        "0001-acpi-gcp-block-aml-systemmemory-ram-access.patch",
    ]


BOX = Container("box", ["/usr/bin/busybox", "true"], packages=["busybox"])


@pytest.mark.parametrize(
    ("image", "patches", "fragments"),
    [
        ({}, [], []),
        ({"targets": ["qemu", "azure"]}, [], []),
        ({"targets": ["gcp"]}, ["0001-acpi-gcp-block-aml-systemmemory-ram-access.patch"], []),
        ({"rootfs": RootFS.verity()}, [], [VERITY_KERNEL]),
        ({"profiles": {"dev": Profile(add=[BOX])}}, [], [CONTAINER_KERNEL]),
    ],
    ids=["qemu", "azure", "gcp", "verity", "container"],
)
def test_the_root_targets_and_containers_add_their_own(
    image: dict[str, object], patches: list[str], fragments: list[KernelFragment]
) -> None:
    lowered = kernel_of(Image("k", kernel=Kernel.build(base=BASE), **image))  # type: ignore[arg-type]
    assert [patch.name for patch in lowered.patches] == patches
    assert [(f.options, f.reason) for f in lowered.fragments] == [
        (f.options, f.reason) for f in fragments
    ]


def test_a_profile_cannot_change_the_shared_kernel() -> None:
    image = Image("k", kernel=Kernel.build(base=BASE), profiles={"xfs": Profile(add=[_Xfs()])})
    with pytest.raises(ValidationError, match="profile 'xfs' adds") as raised:
        kernel_of(image)
    assert "Image(components=" in (raised.value.hint or "")
