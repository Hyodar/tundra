"""Core TDX quote service module usage."""

from tdx import Image
from tdx.modules import Tdxs


def build_with_tdxs() -> None:
    img = Image(base="debian/bookworm", arch="x86_64")
    img.install("ca-certificates")
    img.output_targets("qemu")

    # Module sets up build packages (golang, git), build hook (clone + compile),
    # config.yaml, systemd units, user/group creation, and socket enablement.
    Tdxs(issuer_type="dcap").apply(img)

    img.lock()
    img.bake(frozen=True)


if __name__ == "__main__":
    build_with_tdxs()
