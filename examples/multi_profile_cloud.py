"""Multi-profile cloud recipe example."""

from tdx import Image


def build_cloud_profiles() -> None:
    img = Image(lima_cpus=6, lima_memory="12GiB", lima_disk="100GiB")

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
