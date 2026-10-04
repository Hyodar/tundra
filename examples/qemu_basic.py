"""Minimal QEMU-focused recipe.

tundravm inspect examples/qemu_basic.py
tundravm bake examples/qemu_basic.py
"""

from tundravm.backends import LimaMkosiBackend
from tundravm.declarative import File, Fragment, Package, Recipe

recipe = Recipe(
    name="qemu-basic",
    base="debian/bookworm",
    common=Fragment(
        "qemu-basic",
        items=(Package("curl"), Package("jq"), File("/etc/motd", "QEMU profile\n")),
    ),
)

backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
