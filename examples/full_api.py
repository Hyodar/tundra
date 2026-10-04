"""End-to-end example covering the major SDK API surfaces.

Run it directly, or drive it with the CLI:

    tundravm check examples/full_api.py --all-profiles
    tundravm bake examples/full_api.py --lock --all-profiles
"""

from pathlib import Path

from tundravm import Image, Kernel, MkosiOptions, SecretSchema, SecretTarget
from tundravm.backends import LimaMkosiBackend
from tundravm.modules import (
    DiskEncryption,
    DiskSpec,
    KeyGeneration,
    KeySpec,
    SecretDelivery,
    SecretSpec,
    Tdxs,
)


def build() -> Image:
    img = Image(
        build_dir=Path("build"),
        base="debian/bookworm",
        arch="x86_64",
        reproducible=True,
        backend=LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB"),
        kernel=Kernel.tdx_kernel("6.8"),
        mkosi=MkosiOptions(package_cache_directory="mkosi.cache"),
    )

    img.repository(
        "https://deb.debian.org/debian-security",
        name="debian-security",
        suite="bookworm-security",
        components=["main"],
        priority=10,
    )

    img.install("ca-certificates", "curl", "jq")
    img.targets("qemu")
    img.debloat(
        enabled=True,
        extra_keep_units=["systemd-resolved.service"],
    )

    img.file("/etc/motd", content="TDX VM\n")
    img.template(
        "/etc/app/runtime.env",
        template="NETWORK={network}\nRPC_PORT={rpc_port}\n",
        variables={"network": "mainnet", "rpc_port": 8545},
    )

    img.user("app", system=True, home="/var/lib/app", uid=1000, groups=["tdx"])
    img.service(
        "app.service",
        command=["/usr/local/bin/app", "--config", "/etc/app/runtime.env"],
        user="app",
        after=["network-online.target"],
        restart="always",
        extra_unit={"Service": {"MemoryMax": "4G"}},
        security_profile="strict",
    )

    img.partition("data", size="8G", mount_at="/var/lib/app", fs="ext4")
    img.shell("pip install pyyaml", phase="prepare")
    img.shell("sysctl --system", phase="postinst")  # default phase is postinst
    img.shell("git submodule update --init", phase="sync")

    # Composable init modules
    key = KeySpec("key_persistent", strategy="tpm", output="/persistent/key")
    KeyGeneration(keys=(key,)).apply(img)  # runtime-init priority 10

    disk = DiskSpec("disk_persistent", device="/dev/vda3", key=key, key_name="key_persistent")
    DiskEncryption(disks=(disk,)).apply(img)  # priority 20

    jwt = SecretSpec(
        "jwt_secret",
        required=True,
        schema=SecretSchema(kind="string", min_length=64, max_length=64),
        targets=(
            SecretTarget.file("/run/tdx-secrets/jwt.hex", owner="app", mode="0440"),
            SecretTarget.env("JWT_SECRET", scope="global"),
        ),
    )
    SecretDelivery(secrets=(jwt,), method="http_post").apply(img)  # priority 30

    Tdxs().apply(img)

    with img.profile("azure"):
        img.targets("azure")
        img.install("waagent")

    with img.profile("gcp"):
        img.targets("gcp")
        img.install("google-guest-agent")

    with img.profile("dev"):
        img.install("dropbear")
        img.install("strace", "gdb", "vim")
        img.debloat(enabled=False)

    return img


if __name__ == "__main__":
    img = build()
    img.lock()
    img.bake(frozen=True)
    print(img.measure(backend="rtmr", allow_placeholder=True).to_json())
    print(img.deploy(target="qemu").deployment_id)
