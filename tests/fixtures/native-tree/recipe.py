"""Gate 1 fixture: the smallest recipe that exercises every node of the mkosi-native tree.

One service, one component, a ``dev`` variant that adds content (a profile overlay with
SSH access by public key only) and two variants that only add outputs (the ``azure`` and
``gcp`` wrapper subimages). ``recipe_with(root=...)`` builds it with either root layout;
``tests/compiler/test_native_tree.py`` compiles both into ``tree-initramfs/`` and
``tree-verity/`` beside this file and asserts every file and the resolved mkosi summary.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

from tundravm.declarative import (
    Debian,
    Declaration,
    File,
    Mkosi,
    Package,
    Recipe,
    Service,
    Variant,
    component,
)
from tundravm.declarative.utils import Composite, SshAccess

SNAPSHOT = "20251113T083151Z"
SEED = "6a5f3c1e-2b7d-4e9a-8c10-5d3f2a1b9e70"
DEV_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureFixtureFixtureFixtureFixtureFixture dev@fixture"
)


@component
class Greeter(Composite):
    """The component: the greeting a service reads, and the package it needs."""

    greeting: str = "hello from the payload"

    def compose(self) -> Iterator[Declaration]:
        yield Package("curl")
        yield File("/etc/greeter/greeting", f"{self.greeting}\n")


def recipe_with(root: Literal["initramfs", "verity"] = "initramfs") -> Recipe:
    """The fixture recipe with the payload's root filesystem in *root*."""
    return Recipe(
        name="fixture",
        platform=Debian("trixie", snapshot=SNAPSHOT),
        mkosi=Mkosi(seed=SEED, root=root),
        items=(
            Greeter(),
            Service("greeter", ("/usr/bin/cat", "/etc/greeter/greeting")),
        ),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("azure", outputs=("azure",)),
            Variant("gcp", outputs=("gcp",)),
            Variant("dev", add=SshAccess(authorized_keys=(DEV_KEY,)), release=False),
        ),
    )


recipe = recipe_with()
