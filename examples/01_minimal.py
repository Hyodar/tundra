"""The smallest useful recipe: a few packages and one file in a QEMU image.

Teaches: a ``Recipe`` is a name, a ``platform`` and its ``items``. The platform
``Debian("bookworm")`` is the distribution plus its boot set (the kernel, systemd as
init, udev and the UKI's EFI stub; ``recipe.platform.packages`` lists them); the
items are the declarations every variant shares (``packages()`` bundles ``Package``
declarations). With no ``variants`` it builds one ``default`` variant for QEMU.

    tundravm inspect examples/01_minimal.py
    tundravm compile examples/01_minimal.py --tree build/minimal/mkosi
    tundravm bake examples/01_minimal.py --build build/minimal
"""

from tundravm.backends import InProcessBackend
from tundravm.declarative import Debian, File, Recipe, packages

recipe = Recipe(
    name="minimal",
    platform=Debian("bookworm"),
    items=(
        packages("curl", "jq"),
        File("/etc/motd", "Hello from tundravm\n"),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes a real image
