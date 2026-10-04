"""Minimal QEMU-focused recipe.

Run it directly, or drive it with the CLI:

    tundravm explain examples/qemu_basic.py
    tundravm bake examples/qemu_basic.py --lock
"""

from tundravm import Image
from tundravm.backends import LimaMkosiBackend


def build() -> Image:
    img = Image(backend=LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB"))
    img.install("curl", "jq")
    img.file("/etc/motd", content="QEMU profile\n")
    img.output_targets("qemu")
    return img


if __name__ == "__main__":
    img = build()
    img.lock()
    img.bake(frozen=True)
