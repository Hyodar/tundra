"""Minimal QEMU-focused recipe."""

from tundravm import Image
from tundravm.backends import LimaMkosiBackend


def build_qemu_image() -> None:
    img = Image(backend=LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB"))
    img.install("curl", "jq")
    img.file("/etc/motd", content="QEMU profile\n")
    img.output_targets("qemu")
    img.lock()
    img.bake(frozen=True)


if __name__ == "__main__":
    build_qemu_image()
