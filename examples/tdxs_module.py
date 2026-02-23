"""Core TDX quote service module usage."""

from tdx import Image
from tdx.modules import Tdxs


def build_with_tdxs() -> None:
    img = Image(base="debian/bookworm", arch="x86_64")
    img.install("ca-certificates")
    img.output_targets("qemu")

    # Module declarations are recipe-only; no filesystem side effects until lock/emit/bake.
    Tdxs(issuer_type="dcap").apply(img)

    img.lock()
    img.bake(frozen=True)


if __name__ == "__main__":
    build_with_tdxs()
