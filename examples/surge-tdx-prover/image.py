"""Surge TDX Prover: the full NethermindEth/nethermind-tdx image as one declarative recipe.

``recipe`` is the nethermind-tdx platform and base layer plus the prover stack (Raiko, the Taiko
client and Nethermind, all built from source), a TPM-sealed key, an encrypted
``/persistent`` disk and secrets delivered over HTTP. ``default`` targets QEMU;
``azure`` and ``gcp`` add their platform glue; ``devtools`` adds debugging access.
See the result with ``tundravm inspect examples/surge-tdx-prover/image.py --variant azure``.
"""

from __future__ import annotations

from examples.fragments import Nethermind, Raiko, TaikoClient
from examples.nethermind_base import NETHERMIND_V1, NethermindBase, NethermindPlatform

from tundravm.backends import LimaMkosiBackend
from tundravm.declarative import (
    Disk,
    File,
    Fragment,
    Key,
    Package,
    Policy,
    Recipe,
    Resources,
    Secrets,
    Unit,
    Variant,
)
from tundravm.declarative.utils import DevTools, Run

RUNTIME_PACKAGES = (
    "prometheus",
    "prometheus-node-exporter",
    "prometheus-process-exporter",
    "rclone",
    "libsnappy1v5",
    "openntpd",
    "bubblewrap",
    "dropbear",
    "iptables",
    "iproute2",
    "socat",
    "conntrack",
    "netfilter-persistent",
    "curl",
    "jq",
    "ncat",
    "logrotate",
    "sudo",
    "uidmap",
    "passt",
    "fuse-overlayfs",
    "cryptsetup",
    "openssh-sftp-server",
    "udev",
    "pkg-config",
    "libtss2-dev",
)

BUILD_PACKAGES = (
    "dotnet-sdk-10.0",
    "dotnet-runtime-10.0",
    "golang",
    "libleveldb-dev",
    "libsnappy-dev",
    "zlib1g-dev",
    "libzstd-dev",
    "libpq-dev",
    "libssl-dev",
    "libtss2-dev",
    "build-essential",
    "pkg-config",
    "git",
    "gcc",
)

# Host files under files/, copied to the same path in the image (mode 0644).
ASSETS = Resources.from_module(__name__)
SYSTEM_FILES = (
    "/etc/default/dropbear",
    "/etc/sysctl.d/99-surge.conf",
    "/etc/udev/rules.d/65-tdx-guest.rules",
    "/etc/udev/rules.d/99-tdx-symlink.rules",
    "/etc/openntpd/ntpd.conf",
    "/etc/default/prometheus",
)

# Boot-time init: TPM-sealed key -> encrypted /persistent -> secrets over HTTP.
key = Key("key_persistent", output="/tmp/key_persistent")
disk = Disk(
    "disk_persistent",
    mount="/persistent",
    device=None,  # the largest unpartitioned disk: Policy(storage_safety="warn") below
    key=key,
    mapper="cryptroot",
    format="on_fail",
)

prover_stack = Fragment(
    "prover-stack",
    items=(
        # The historical tree runs groupadd itself; a new recipe declares Group("eth").
        Run("groupadd -r eth", name="eth-group"),
        Raiko(),
        TaikoClient(),
        Nethermind(),
        # Nethermind reads the TDX devices too; the historical tree grants it with usermod.
        Run("usermod -a -G tdx nethermind-surge", name="nethermind-tdx-group"),
    ),
)

system = Fragment(
    "system",
    items=(
        *(File.copy(path, ASSETS.file(f"files{path}")) for path in SYSTEM_FILES),
        Unit("network-setup.service", enabled=True),
        Unit("openntpd.service", enabled=True),
        Unit("logrotate.service", enabled=True),
        Unit("dropbear.service", enabled=True),
        # dropbear owns port 22
        Unit("ssh.service", enabled=False, masked=True),
        Unit("ssh.socket", enabled=False, masked=True),
    ),
)

recipe = Recipe(
    name="surge-tdx-prover",
    platform=NethermindPlatform(),
    mkosi=NETHERMIND_V1,
    # The historical image formats the disk disk-setup picks at boot (disk-device-guessed)
    policy=Policy(storage_safety="warn"),
    items=(
        NethermindBase(),
        *(Package(name) for name in RUNTIME_PACKAGES),
        *(Package(name, role="build") for name in BUILD_PACKAGES),
        key,
        disk,
        Secrets(store=disk),
        prover_stack,
        system,
    ),
    variants=(
        Variant("default", outputs=("qemu",)),
        Variant("azure", inherits="default", outputs=("azure",)),
        Variant("gcp", inherits="default", outputs=("gcp",)),
        Variant("devtools", inherits="default", add=DevTools(reproducible=False)),  # never ship it
    ),
)

backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
