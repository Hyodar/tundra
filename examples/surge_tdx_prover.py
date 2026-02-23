"""Surge TDX Prover — full NethermindEth/nethermind-tdx image.

This example composes the complete surge-tdx-prover image on top of the
nethermind-tdx base layer, achieving behavioral equivalence with the
upstream NethermindEth/nethermind-tdx repository.

Layers:
  1. Base layer (build_nethermind_base): Debian Trixie, kernel build,
     reproducibility hooks, debloat, Tdxs quote service
  2. Application layer: TdxInit, Raiko, TaikoClient, Nethermind modules,
     plus monitoring, networking, and platform profiles

The SDK's integration tests verify this configuration produces output that
matches the upstream repo across all directories: root configs, base/,
kernel/, surge-tdx-prover/, services/, and profiles/.
"""

from __future__ import annotations

from examples.nethermind_tdx import build_nethermind_base

from tdx import Image
from tdx.modules import Nethermind, Raiko, TaikoClient, TdxInit
from tdx.profiles import apply_azure_profile, apply_devtools_profile, apply_gcp_profile

# ── Upstream constants ────────────────────────────────────────────────

PINNED_MIRROR = "https://snapshot.debian.org/archive/debian/20251113T083151Z/"

# ── Dropbear SSH configuration ────────────────────────────────────────

DROPBEAR_CONFIG = """\
DROPBEAR_PORT=22
DROPBEAR_EXTRA_ARGS="-s -w -g"
NO_START=0"""

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
vm.max_map_count=262144

# File descriptor limits
fs.file-max=1048576"""

# ── TDX guest udev rule ──────────────────────────────────────────────

TDX_GUEST_UDEV_RULE = """\
# TDX guest device permissions
KERNEL=="tdx_guest", MODE="0660", GROUP="tdx"
KERNEL=="tdx-guest", MODE="0660", GROUP="tdx"
KERNEL=="tpm0", MODE="0660", GROUP="tdx"
KERNEL=="tpmrm0", MODE="0660", GROUP="tdx"
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
      - TdxInit, Raiko, TaikoClient, Nethermind modules
      - System config files (dropbear, sysctl, udev, env files)
      - Azure, GCP, and devtools platform profiles
    """
    # ── 1. Base layer ─────────────────────────────────────────────────
    img = build_nethermind_base()

    # ── 2. Application-layer packages ─────────────────────────────────
    img.install(*RUNTIME_PACKAGES)
    img.build_install(*BUILD_PACKAGES)

    # ── 3. Service modules ────────────────────────────────────────────

    # TDX Init: runtime initialization with SSH, key, and disk config
    TdxInit(
        ssh_strategy="webserver",
        key_strategy="tpm",
        disk_strategy="luks",
        mount_point="/persistent",
        runtime_users=(
            "nethermind-surge",
            "raiko",
            "taiko-client",
        ),
        runtime_directories=(
            "/persistent/nethermind",
            "/persistent/raiko",
            "/persistent/taiko-client",
            "/persistent/jwt",
        ),
        runtime_devices=(
            "/dev/tpm0",
            "/dev/tdx_guest",
        ),
    ).apply(img)

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

    # ── 4. Config files & systemd units ───────────────────────────────

    # Dropbear SSH daemon configuration
    img.file("/etc/default/dropbear", content=DROPBEAR_CONFIG)

    # Sysctl hardening
    img.file("/etc/sysctl.d/99-surge.conf", content=SYSCTL_CONF)

    # TDX guest device permissions
    img.file(
        "/etc/udev/rules.d/65-tdx-guest.rules",
        content=TDX_GUEST_UDEV_RULE,
    )

    # OpenNTPD configuration
    img.file("/etc/openntpd/ntpd.conf", content=OPENNTPD_CONF)

    # Prometheus defaults
    img.file("/etc/default/prometheus", content=PROMETHEUS_DEFAULTS)

    # Service environment files
    img.file("/etc/nethermind-surge/env", content=NETHERMIND_ENV)
    img.file("/etc/raiko/env", content=RAIKO_ENV)
    img.file("/etc/taiko-client/env", content=TAIKO_CLIENT_ENV)

    # Enable services in postinst
    img.run(
        "mkosi-chroot", "systemctl", "enable",
        "prometheus.service",
        phase="postinst",
    )
    img.run(
        "mkosi-chroot", "systemctl", "enable",
        "prometheus-node-exporter.service",
        phase="postinst",
    )
    img.run(
        "mkosi-chroot", "systemctl", "enable",
        "openntpd.service",
        phase="postinst",
    )
    img.run(
        "mkosi-chroot", "systemctl", "enable",
        "dropbear.service",
        phase="postinst",
    )

    # Create eth group (shared by nethermind-surge and taiko-client)
    img.run(
        "mkosi-chroot", "bash", "-c",
        "getent group eth >/dev/null 2>&1 || groupadd -r eth",
        phase="postinst",
    )

    # ── 5. Platform profiles ──────────────────────────────────────────

    with img.profile("azure"):
        apply_azure_profile(img)

    with img.profile("gcp"):
        apply_gcp_profile(img)

    with img.profile("devtools"):
        apply_devtools_profile(img)

    return img


if __name__ == "__main__":
    img = build_surge_tdx_prover()
    img.compile("build/mkosi")
    img.lock()
    img.bake(frozen=True)
