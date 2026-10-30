"""Variants: one recipe, one payload, each variant an addition to it.

Teaches ``Variant``: ``outputs`` for the targets it builds (a cloud wrapper around the same
UKI), ``add`` a fragment and ``changes`` that edit an inherited declaration by its identity
(its type plus natural key) through its ref: ``motd.ref`` for a value you hold. A variant
that adds or edits content compiles to an mkosi profile overlay under
``payload/mkosi.profiles/<name>/``, so it can add, and replace files, repositories and the
debloat; removing what the payload declares is a different image, i.e. another recipe.
``packages()`` bundles ``Package`` declarations without inventing a fragment name.

    tundravm inspect examples/02_variants.py --diff-variants default dev
    tundravm compile examples/02_variants.py --tree build/variants/mkosi
    tundravm bake examples/02_variants.py --variant cloud --build build/variants
"""

from tundravm.backends import InProcessBackend
from tundravm.declarative import Debian, Debloat, File, Recipe, Variant, packages

motd = File("/etc/motd", "production\n")
debloat = Debloat(keep_modules=True)  # Debian's kernel loads its drivers from /usr/lib/modules

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
        # One UKI wrapped twice, an Azure VHD and a GCP image: its cloud glue is gated at
        # boot (ConditionVirtualization=), so one set of measurements covers both.
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
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes real images
