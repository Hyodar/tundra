"""End-to-end example covering the major declaration types.

Drive it with the CLI:

    tundravm check examples/full_api.py
    tundravm bake examples/full_api.py
"""

from tundravm.backends import LimaMkosiBackend
from tundravm.declarative import (
    Debloat,
    Disk,
    File,
    Fragment,
    Git,
    Hook,
    Kernel,
    Key,
    Package,
    Partition,
    Recipe,
    Repository,
    Schema,
    Secret,
    SecretEnv,
    SecretFile,
    Secrets,
    Setting,
    Unit,
    User,
    Variant,
    tdxs,
)

APP_SERVICE = """\
[Unit]
Description=app
After=network-online.target

[Service]
User=app
ExecStart=/usr/local/bin/app --config /etc/app/runtime.env
Restart=always
MemoryMax=4G
NoNewPrivileges=yes
ProtectSystem=strict

[Install]
WantedBy=minimal.target
"""

key = Key("key_persistent", output="/persistent/key")
disk = Disk("disk_persistent", mount="/persistent", device="/dev/vda3", key=key)
jwt = Secret(
    "jwt_secret",
    targets=(
        SecretFile("/run/tdx-secrets/jwt.hex", mode=0o440, owner="app"),
        SecretEnv("JWT_SECRET"),
    ),
    schema=Schema(kind="string", min_length=64, max_length=64),
)

recipe = Recipe(
    name="full-api",
    base="debian/bookworm",
    common=Fragment(
        "app",
        items=(
            Kernel("6.8", Git("https://github.com/gregkh/linux", "v6.8")),
            Setting("Build", "PackageCacheDirectory", ("mkosi.cache",)),
            Repository(
                "debian-security",
                "https://deb.debian.org/debian-security",
                suite="bookworm-security",
                priority=10,
            ),
            *(Package(name) for name in ("ca-certificates", "curl", "jq")),
            Debloat(),
            File("/etc/motd", "TDX VM\n"),
            File(
                "/etc/app/runtime.env",
                "NETWORK={network}\nRPC_PORT={port}\n".format(network="mainnet", port=8545),
            ),
            tdxs(),
            User("app", home="/var/lib/app", uid=1000, groups=("tdx",)),
            Unit("app.service", APP_SERVICE, enabled=True, after_init=True),
            Partition("data", size="8G", mount="/var/lib/app"),
            Hook("pyyaml", "prepare", "pip install pyyaml"),
            Hook("sysctl", "postinst", "sysctl --system"),
            Hook("submodules", "sync", "git submodule update --init"),
            key,
            disk,
            Secrets(entries=(jwt,), store=disk),
        ),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant("azure", target="azure", add=Fragment("azure", items=(Package("waagent"),))),
        Variant("gcp", target="gcp", add=Fragment("gcp", items=(Package("google-guest-agent"),))),
        Variant(
            "dev",
            add=Fragment(
                "dev", items=tuple(Package(n) for n in ("dropbear", "strace", "gdb", "vim"))
            ),
            replace=(Debloat(enabled=False),),
        ),
    ),
)

backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
