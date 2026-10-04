"""The smallest useful recipe: a few packages and one file in a QEMU image.

Teaches: a ``Recipe`` is a name, a base distribution and a ``common`` fragment of
declarations; with no ``variants`` it builds one ``default`` variant for QEMU.

    tundravm inspect examples/01_minimal.py
    tundravm compile examples/01_minimal.py --out build/minimal/mkosi
    tundravm bake examples/01_minimal.py --out build/minimal
"""

from tundravm.backends import InProcessBackend
from tundravm.declarative import File, Fragment, Package, Recipe

# Boot: the distribution kernel, systemd as init, udev and the UKI's EFI stub.
BOOT = ("linux-image-amd64", "systemd", "systemd-sysv", "udev", "kmod", "systemd-boot-efi")

recipe = Recipe(
    name="minimal",
    base="debian/bookworm",
    common=Fragment(
        "base",
        items=(
            *(Package(name) for name in (*BOOT, "curl", "jq")),
            File("/etc/motd", "Hello from tundravm\n"),
        ),
    ),
)

backend = InProcessBackend()  # simulated artifacts; LimaMkosiBackend() bakes a real image
