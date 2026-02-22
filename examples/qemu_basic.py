"""Minimal QEMU-focused recipe."""

from tdx import Image


def build_qemu_image() -> None:
    img = Image()
    img.install("curl", "jq")
    img.file("/etc/motd", content="QEMU profile\n")
    img.output_targets("qemu")
    img.lock()
    img.bake(frozen=True)


if __name__ == "__main__":
    build_qemu_image()
