"""Core TDX quote service module usage.

tundravm explain examples/tdxs_module.py
"""

from tundravm import Image
from tundravm.backends import LimaMkosiBackend
from tundravm.modules import Tdxs


def build() -> Image:
    img = Image(
        base="debian/bookworm",
        arch="x86_64",
        backend=LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB"),
    )
    img.install("ca-certificates")
    img.targets("qemu")

    # Module sets up build packages (golang, git), build hook (clone + compile),
    # config.yaml, systemd units, user/group creation, and socket enablement.
    img.apply(Tdxs())
    return img


if __name__ == "__main__":
    img = build()
    img.lock()
    img.bake(frozen=True)
