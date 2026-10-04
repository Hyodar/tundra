"""Variants: one recipe, several images, each an overlay on its parent.

Teaches ``Variant``: ``add`` a fragment; ``replace`` or ``remove`` a declaration by its
identity (its type plus natural key: ``Package("jq")``, ``File("/etc/motd", ...)``, the
one ``Debloat``); ``parent`` chains (the default ``"base"`` is ``Recipe.common``,
``None`` starts from nothing); ``target`` for one output, ``targets`` for several.

    tundravm inspect examples/02_variants.py --diff-variants default dev
    tundravm compile examples/02_variants.py --out build/variants/mkosi
    tundravm bake examples/02_variants.py --variant cloud --out build/variants
"""

from tundravm.backends import InProcessBackend
from tundravm.declarative import Debloat, File, Fragment, Package, Recipe, Variant

BOOT = ("linux-image-amd64", "systemd", "systemd-sysv", "udev", "kmod", "systemd-boot-efi")

recipe = Recipe(
    name="variants",
    base="debian/bookworm",
    common=Fragment(
        "base",
        items=(
            *(Package(name) for name in BOOT),
            Package("ca-certificates"),
            Package("curl"),
            Package("jq"),
            File("/etc/motd", "production\n"),
            Debloat(),
        ),
    ),
    variants=(
        # Recipe.common as is, built for QEMU.
        Variant("default", target="qemu"),
        # The same tree twice: an Azure VHD and a GCP image from one variant.
        Variant("cloud", parent="default", targets=("azure", "gcp")),
        # Inherits default's target; adds tools, swaps the motd and keeps systemd whole.
        Variant(
            "dev",
            parent="default",
            add=Fragment("dev-tools", items=(Package("strace"), Package("gdb"))),
            replace=(File("/etc/motd", "development build\n"), Debloat(enabled=False)),
        ),
        # default minus jq.
        Variant("slim", parent="default", remove=(Package("jq"),)),
        # Standalone: none of Recipe.common, e.g. a rescue image.
        Variant(
            "rescue",
            parent=None,
            target="qemu",
            add=Fragment(
                "rescue",
                items=(*(Package(name) for name in BOOT), Package("busybox"), Package("e2fsprogs")),
            ),
        ),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes real images
