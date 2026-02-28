"""Surge TDX Prover — full NethermindEth/nethermind-tdx image.

This example composes the complete surge-tdx-prover image on top of the
nethermind-tdx base layer, achieving behavioral equivalence with the
upstream NethermindEth/nethermind-tdx repository.

Layers:
  1. Base layer (build_nethermind_base): Debian Trixie, kernel build,
     reproducibility hooks, debloat, Tdxs quote service
  2. Application layer: Init with composable modules (KeyGeneration,
     DiskEncryption, SecretDelivery), Raiko, TaikoClient, Nethermind,
     plus monitoring, networking, and platform profiles

The SDK's integration tests verify this configuration produces output that
matches the upstream repo across all directories: root configs, base/,
kernel/, surge-tdx-prover/, services/, and profiles/.
"""

from __future__ import annotations

from examples.modules import Nethermind, Raiko, TaikoClient
from examples.nethermind_tdx import build_nethermind_base

from tundravm import Image
from tundravm.modules import (
    Devtools,
    DiskEncryption,
    KeyGeneration,
    SecretDelivery,
)
from tundravm.platforms import AzurePlatform, GcpPlatform

# ── Upstream constants ────────────────────────────────────────────────

PINNED_MIRROR = "https://snapshot.debian.org/archive/debian/20251113T083151Z/"

# ── Dropbear SSH configuration ────────────────────────────────────────

DROPBEAR_CONFIG = """\
DROPBEAR_EXTRA_ARGS="-s -w -g -m -j -k"
DROPBEAR_RECEIVE_WINDOW=6291456
DROPBEAR_PORT=0.0.0.0:22
DROPBEAR_SUBSYSTEM="sftp /usr/lib/openssh/sftp-server"
"""

# ── Sysctl hardening ─────────────────────────────────────────────────

SYSCTL_CONF = """\
# Network hardening
net.ipv4.ip_forward=1
net.ipv4.conf.all.forwarding=1
net.ipv6.conf.all.forwarding=1
net.ipv4.tcp_syncookies=1
net.ipv4.conf.all.accept_redirects=0
net.ipv4.conf.default.accept_redirects=0
net.ipv6.conf.all.accept_redirects=0
net.ipv6.conf.default.accept_redirects=0
net.ipv4.conf.all.send_redirects=0
net.ipv4.conf.default.send_redirects=0
net.ipv4.conf.all.rp_filter=1
net.ipv4.conf.default.rp_filter=1

# VM tuning
vm.swappiness=1
vm.max_map_count=2097152

# File descriptor limits
fs.file-max=1048576"""

# ── TDX guest udev rules ─────────────────────────────────────────────

TDX_GUEST_PERMISSIONS = """\
# TDX guest device permissions
KERNEL=="tdx_guest", MODE="0660", GROUP="tdx"
KERNEL=="tdx-guest", MODE="0660", GROUP="tdx"
KERNEL=="tpm0", MODE="0660", GROUP="tdx"
KERNEL=="tpmrm0", MODE="0660", GROUP="tdx"
"""

TDX_GUEST_SYMLINK = """\
KERNEL=="tdx_guest", SYMLINK+="tdx-guest"
"""

# ── OpenNTPD configuration ───────────────────────────────────────────

OPENNTPD_CONF = """\
servers pool.ntp.org
sensor *
constraints from "https://www.google.com/"""

# ── Prometheus configuration ─────────────────────────────────────────

PROMETHEUS_DEFAULTS = (
    'ARGS="'
    "--config.file=/etc/prometheus/prometheus.yml "
    "--storage.tsdb.path=/var/lib/prometheus "
    "--web.listen-address=127.0.0.1:9090 "
    '--storage.tsdb.retention.time=7d"\n'
)

# ── Environment files ────────────────────────────────────────────────

NETHERMIND_ENV = """\
NETHERMIND_CONFIG=/etc/nethermind-surge/config.json
NETHERMIND_DATADIR=/persistent/nethermind
NETHERMIND_JSONRPC_ENGINEHOST=127.0.0.1
NETHERMIND_JSONRPC_ENGINEPORT=8551
NETHERMIND_JSONRPC_HOST=127.0.0.1
NETHERMIND_JSONRPC_PORT=8545
NETHERMIND_JSONRPC_JWTSECRETFILE=/persistent/jwt/jwt.hex"""

RAIKO_ENV = """\
RAIKO_CONFIG=/etc/raiko/config.json
RAIKO_CHAIN_SPEC=/etc/raiko/chain-spec.json"""

TAIKO_CLIENT_ENV = """\
TAIKO_CLIENT_CONFIG=/etc/taiko-client/config.json"""

# ── Runtime packages (upstream surge-tdx-prover) ─────────────────────

RUNTIME_PACKAGES: tuple[str, ...] = (
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

# ── Build packages (upstream surge-tdx-prover) ───────────────────────

BUILD_PACKAGES: tuple[str, ...] = (
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


# ── Image definition ──────────────────────────────────────────────────


def build_surge_tdx_prover() -> Image:
    """Build the complete surge-tdx-prover image.

    Composes:
      - Base layer (nethermind-tdx base: kernel, debloat, Tdxs)
      - Application layer runtime + build packages
      - Init with composable modules (KeyGeneration, DiskEncryption, SecretDelivery)
      - Raiko, TaikoClient, Nethermind service modules
      - System config files (dropbear, sysctl, udev, env files)
      - Azure, GCP, and devtools platform profiles
    """
    # ── 1. Base layer ─────────────────────────────────────────────────
    img = build_nethermind_base()

    # Reproducibility: pin Debian snapshot mirror
    img.mirror = PINNED_MIRROR
    img.tools_tree_mirror = PINNED_MIRROR

    # ── 2. Application-layer packages ─────────────────────────────────
    img.install(*RUNTIME_PACKAGES)
    img.build_install(*BUILD_PACKAGES)

    # ── 3. Composable init modules ───────────────────────────────────

    keys = KeyGeneration()
    keys.key("key_persistent", strategy="tpm", output="/tmp/key_persistent")
    keys.apply(img)

    disks = DiskEncryption()
    disks.disk(
        "disk_persistent",
        device=None,
        key_path="/tmp/key_persistent",
        mapper_name="cryptroot",
        mount_point="/persistent",
    )
    disks.apply(img)
    SecretDelivery(method="http_post").apply(img)

    # ── 4. Groups and service modules ─────────────────────────────────
    # Create groups BEFORE modules that reference them (upstream pattern)
    img.run("mkosi-chroot groupadd -r eth", phase="postinst")

    # Raiko: TDX prover service (Rust)
    Raiko(
        source_repo="https://github.com/NethermindEth/raiko.git",
        source_branch="feat/tdx",
    ).apply(img)

    # TaikoClient: Go-based client
    TaikoClient(
        source_repo="https://github.com/NethermindEth/surge-taiko-mono",
        source_branch="feat/tdx-proving",
        build_path="packages/taiko-client",
    ).apply(img)

    # Nethermind: .NET execution client
    Nethermind(
        source_repo="https://github.com/NethermindEth/nethermind.git",
        version="1.32.3",
    ).apply(img)

    # Add nethermind-surge to tdx group (upstream has it in both eth and tdx)
    img.run("mkosi-chroot usermod -a -G tdx nethermind-surge", phase="postinst")

    # ── 5. Config files & systemd units ───────────────────────────────

    # Dropbear SSH daemon configuration
    img.file("/etc/default/dropbear", content=DROPBEAR_CONFIG)

    # Sysctl hardening
    img.file("/etc/sysctl.d/99-surge.conf", content=SYSCTL_CONF)

    # TDX guest udev rules (permissions + symlink)
    img.file("/etc/udev/rules.d/65-tdx-guest.rules", content=TDX_GUEST_PERMISSIONS)
    img.file("/etc/udev/rules.d/99-tdx-symlink.rules", content=TDX_GUEST_SYMLINK)

    # OpenNTPD configuration
    img.file("/etc/openntpd/ntpd.conf", content=OPENNTPD_CONF)

    # Prometheus defaults
    img.file("/etc/default/prometheus", content=PROMETHEUS_DEFAULTS)

    # Service environment files
    img.file("/etc/nethermind-surge/env", content=NETHERMIND_ENV)
    img.file("/etc/raiko/env", content=RAIKO_ENV)
    img.file("/etc/taiko-client/env", content=TAIKO_CLIENT_ENV)

    # ── 6. Package-provided services (modules handle their own) ────────
    img.service("network-setup", enabled=True)
    img.service("openntpd", enabled=True)
    img.service("logrotate", enabled=True)
    img.service("dropbear", enabled=True)

    # SSH hardening: free port 22 for dropbear
    img.run("mkosi-chroot systemctl disable ssh.service ssh.socket", phase="postinst")
    img.run("mkosi-chroot systemctl mask ssh.service ssh.socket", phase="postinst")

    # ── 7. Platform profiles ──────────────────────────────────────────

    with img.profile("azure"):
        AzurePlatform().apply(img)

    with img.profile("gcp"):
        GcpPlatform().apply(img)

    with img.profile("devtools"):
        Devtools().apply(img)

    return img
