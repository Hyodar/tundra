"""Multi-profile cloud recipe example."""

from tundravm import Image
from tundravm.backends import LimaMkosiBackend


def build_cloud_profiles() -> None:
    img = Image(backend=LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB"))

    with img.profile("azure"):
        img.install("waagent")
        img.output_targets("azure")

    with img.profile("gcp"):
        img.install("google-guest-agent")
        img.output_targets("gcp")

    with img.profile("qemu"):
        img.install("qemu-guest-agent")
        img.output_targets("qemu")

    with img.all_profiles():
        img.lock()
        img.bake(frozen=True)


if __name__ == "__main__":
    build_cloud_profiles()
