"""Variants: one recipe, several images, each an overlay on the variant it inherits.

Teaches ``Variant``: ``add`` a fragment; ``replace`` or ``remove`` a declaration by its
identity (its type plus natural key: ``Package("jq")``, ``File("/etc/motd", ...)``, the
one ``Debloat``); ``inherits`` chains (the default ``"base"`` is ``Recipe.items``,
``None`` starts from nothing); ``outputs`` for the targets it builds. ``packages()``
bundles ``Package`` declarations without inventing a fragment name.

    tundravm inspect examples/02_variants.py --diff-variants default dev
    tundravm compile examples/02_variants.py --tree build/variants/mkosi
    tundravm bake examples/02_variants.py --variant cloud --build build/variants
"""

from tundravm.backends import InProcessBackend
from tundravm.declarative import Debloat, File, Package, Recipe, Variant, packages

BOOT = ("linux-image-amd64", "systemd", "systemd-sysv", "udev", "kmod", "systemd-boot-efi")

recipe = Recipe(
    name="variants",
    base="debian/bookworm",
    items=(
        packages(*BOOT, "ca-certificates", "curl", "jq"),
        File("/etc/motd", "production\n"),
        Debloat(),
    ),
    variants=(
        # Recipe.items as is, built for QEMU.
        Variant("default", outputs=("qemu",)),
        # The same tree twice: an Azure VHD and a GCP image from one variant.
        Variant("cloud", inherits="default", outputs=("azure", "gcp")),
        # Inherits default's outputs; adds tools, swaps the motd and keeps systemd whole.
        Variant(
            "dev",
            inherits="default",
            add=packages("strace", "gdb"),
            replace=(File("/etc/motd", "development build\n"), Debloat(enabled=False)),
        ),
        # default minus jq.
        Variant("slim", inherits="default", remove=(Package("jq"),)),
        # Standalone: none of Recipe.items, e.g. a rescue image.
        Variant(
            "rescue",
            inherits=None,
            outputs=("qemu",),
            add=packages(*BOOT, "busybox", "e2fsprogs"),
        ),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes real images
