"""Surge TDX Prover: the full NethermindEth/nethermind-tdx image as one declarative recipe.

``recipe`` is the nethermind-tdx base layer plus the prover stack (Raiko, the Taiko
client and Nethermind, all built from source), a TPM-sealed key, an encrypted
``/persistent`` disk and secrets delivered over HTTP. ``default`` targets QEMU;
``azure`` and ``gcp`` add their platform glue; ``devtools`` adds debugging access.
See the result with ``tundravm inspect examples/surge-tdx-prover/image.py --variant azure``.
"""

from __future__ import annotations

from contents import (
    DROPBEAR_CONFIG,
    OPENNTPD_CONF,
    PROMETHEUS_DEFAULTS,
    SYSCTL_CONF,
    TDX_GUEST_PERMISSIONS,
    TDX_GUEST_SYMLINK,
)
from examples.modules import nethermind, raiko, taiko_client
from examples.nethermind_tdx import NETHERMIND_V1, PINNED_MIRROR, nethermind_base

from tundravm.backends import LimaMkosiBackend
from tundravm.declarative import (
    Disk,
    File,
    Fragment,
    Hook,
    Key,
    Package,
    Recipe,
    Secrets,
    Unit,
    Variant,
    devtools,
)

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

# Boot-time init: TPM-sealed key -> encrypted /persistent -> secrets over HTTP.
key = Key("key_persistent", output="/tmp/key_persistent")
disk = Disk(
    "disk_persistent",
    mount="/persistent",
    device=None,  # the largest unpartitioned disk
    key=key,
    mapper="cryptroot",
)

prover_stack = Fragment(
    "prover-stack",
    items=(
        # The historical tree spells this group "-r", unlike the tdx group tdxs() declares.
        Hook("eth-group", "postinst", "mkosi-chroot groupadd -r eth"),
        raiko(),
        taiko_client(),
        nethermind(),
        # Nethermind reads the TDX devices too; the tree grants it with usermod.
        Hook("nethermind-tdx-group", "postinst", "mkosi-chroot usermod -a -G tdx nethermind-surge"),
    ),
)

system = Fragment(
    "system",
    items=(
        File("/etc/default/dropbear", DROPBEAR_CONFIG),
        File("/etc/sysctl.d/99-surge.conf", SYSCTL_CONF),
        File("/etc/udev/rules.d/65-tdx-guest.rules", TDX_GUEST_PERMISSIONS),
        File("/etc/udev/rules.d/99-tdx-symlink.rules", TDX_GUEST_SYMLINK),
        File("/etc/openntpd/ntpd.conf", OPENNTPD_CONF),
        File("/etc/default/prometheus", PROMETHEUS_DEFAULTS),
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
    base="debian/trixie",
    mirror=PINNED_MIRROR,
    tools_mirror=PINNED_MIRROR,
    epoch=0,
    mkosi=NETHERMIND_V1,
    common=Fragment(
        "surge",
        items=(
            nethermind_base(),
            *(Package(name) for name in RUNTIME_PACKAGES),
            *(Package(name, role="build") for name in BUILD_PACKAGES),
            key,
            disk,
            Secrets(store=disk),
            prover_stack,
            system,
        ),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant("azure", parent="default", target="azure"),
        Variant("gcp", parent="default", target="gcp"),
        Variant("devtools", parent="default", add=devtools()),  # never ship it
    ),
)

backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
