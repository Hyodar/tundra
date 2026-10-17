"""Variants: one recipe, several images, each an overlay on the variant it inherits.

Teaches ``Variant``: ``add`` a fragment; ``changes`` that edit or remove an inherited
declaration by its identity (its type plus natural key), through its ref: ``motd.ref`` for
a value you hold, ``Package.ref("jq")`` for one you do not; ``inherits`` chains (the
default ``"base"`` is ``Recipe.items``, ``None`` starts from the platform alone); ``outputs``
for the targets it builds. Every variant, standalone ones too, gets the platform's boot
set. ``packages()`` bundles ``Package`` declarations without inventing a fragment name.

    tundravm inspect examples/02_variants.py --diff-variants default dev
    tundravm compile examples/02_variants.py --tree build/variants/mkosi
    tundravm bake examples/02_variants.py --variant cloud --build build/variants
"""

from tundravm.backends import InProcessBackend
from tundravm.declarative import Debian, Debloat, File, Package, Recipe, Variant, packages

motd = File("/etc/motd", "production\n")
debloat = Debloat()

recipe = Recipe(
    name="variants",
    platform=Debian("bookworm"),
    items=(
        packages("ca-certificates", "curl", "jq"),
        motd,
        debloat,
    ),
    variants=(
        # Recipe.items as is, built for QEMU.
        Variant("default", outputs=("qemu",)),
        # The same tree twice: an Azure VHD and a GCP image from one variant.
        Variant("cloud", inherits="default", outputs=("azure", "gcp")),
        # Inherits default's outputs; adds tools, edits the motd and keeps systemd whole.
        Variant(
            "dev",
            inherits="default",
            add=packages("strace", "gdb"),
            changes=(
                motd.ref.edit(content="development build\n"),
                debloat.ref.edit(enabled=False),
            ),
        ),
        # default minus jq: a ref names a declaration you hold no value for.
        Variant("slim", inherits="default", changes=(Package.ref("jq").remove(),)),
        # Standalone: the platform without Recipe.items, e.g. a rescue image.
        Variant(
            "rescue",
            inherits=None,
            outputs=("qemu",),
            add=packages("busybox", "e2fsprogs"),
        ),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes real images
