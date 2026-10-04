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
    KeyGeneration,
    SecretDelivery,
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
    img.output_targets("qemu")
    img.debloat(
        enabled=True,
        systemd_units_keep_extra=["systemd-resolved.service"],
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

    img.partition("data", size="8G", mount="/var/lib/app", fs="ext4")
    img.prepare("pip install pyyaml")
    img.run("sysctl --system")  # default phase is postinst
    img.sync("git submodule update --init")

    # Composable init modules
    keys = KeyGeneration()
    keys.key("key_persistent", strategy="tpm", output="/persistent/key")  # priority 10
    keys.apply(img)

    disks = DiskEncryption()
    disks.disk(
        "disk_persistent",
        device="/dev/vda3",
        key_name="key_persistent",
        key_path="/persistent/key",
    )  # priority 20
    disks.apply(img)

    # Secret delivery: declare secrets then apply
    delivery = SecretDelivery(method="http_post")
    delivery.secret(
        "jwt_secret",
        required=True,
        schema=SecretSchema(kind="string", min_length=64, max_length=64),
        targets=(
            SecretTarget.file("/run/tdx-secrets/jwt.hex", owner="app", mode="0440"),
            SecretTarget.env("JWT_SECRET", scope="global"),
        ),
    )
    delivery.apply(img)  # priority 30

    Tdxs().apply(img)

    with img.profile("azure"):
        img.output_targets("azure")
        img.install("waagent")

    with img.profile("gcp"):
        img.output_targets("gcp")
        img.install("google-guest-agent")

    with img.profile("dev"):
        img.ssh()
        img.install("strace", "gdb", "vim")
        img.debloat(enabled=False)

    return img


if __name__ == "__main__":
    img = build()
    img.lock()
    img.bake(frozen=True)
    print(img.measure(backend="rtmr", allow_placeholder=True).to_json())
    print(img.deploy(target="qemu").deployment_id)
