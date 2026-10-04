"""Multi-profile cloud recipe example.

Run it directly, or drive it with the CLI:

    tundravm explain examples/multi_profile_cloud.py --all-profiles
    tundravm bake examples/multi_profile_cloud.py --lock --all-profiles
"""

from tundravm import Image
from tundravm.backends import LimaMkosiBackend


def build() -> Image:
    img = Image(backend=LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB"))

    with img.profile("azure"):
        img.install("waagent")
        img.targets("azure")

    with img.profile("gcp"):
        img.install("google-guest-agent")
        img.targets("gcp")

    with img.profile("qemu"):
        img.install("qemu-guest-agent")
        img.targets("qemu")

    return img


if __name__ == "__main__":
    img = build()
    with img.all_profiles():
        img.lock()
        img.bake(frozen=True)
