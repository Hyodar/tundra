"""Surge TDX Prover: the full NethermindEth/nethermind-tdx image as one recipe.

Start at ``build()``: it lists the steps in build order, each a small function below it.
See the result with ``tundravm explain examples/surge-tdx-prover/image.py --profile azure``.
Integration tests check the emitted tree against the upstream repository.
"""

from __future__ import annotations

from examples.modules import Nethermind, Raiko, TaikoClient
from examples.nethermind_tdx import PINNED_MIRROR, build_nethermind_base

from tundravm import Image
from tundravm.modules import Devtools, DiskEncryption, KeyGeneration, SecretDelivery
from tundravm.platforms import AzurePlatform, GcpPlatform

# ── Packages (upstream surge-tdx-prover mkosi.conf) ──────────────────

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

# ── Config file contents (byte-exact with upstream: no trailing newline where absent) ──

DROPBEAR_CONFIG = """\
DROPBEAR_EXTRA_ARGS="-s -w -g -m -j -k"
DROPBEAR_RECEIVE_WINDOW=6291456
DROPBEAR_PORT=0.0.0.0:22
DROPBEAR_SUBSYSTEM="sftp /usr/lib/openssh/sftp-server"
"""

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

OPENNTPD_CONF = """\
servers pool.ntp.org
sensor *
constraints from "https://www.google.com/"""

PROMETHEUS_DEFAULTS = (
    'ARGS="'
    "--config.file=/etc/prometheus/prometheus.yml "
    "--storage.tsdb.path=/var/lib/prometheus "
    "--web.listen-address=127.0.0.1:9090 "
    '--storage.tsdb.retention.time=7d"\n'
)

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


# ── Recipe ────────────────────────────────────────────────────────────


def build() -> Image:
    """The surge-tdx-prover image: the default profile plus azure, gcp and devtools."""
    img = _base()
    _packages(img)
    _boot_init(img)
    _prover_stack(img)
    _system_config(img)
    _system_services(img)
    _cloud_profiles(img)
    _devtools_profile(img)
    return img


def _base() -> Image:
    """The nethermind-tdx base layer, resolving packages from a pinned Debian snapshot."""
    return build_nethermind_base().pin_mirror(PINNED_MIRROR)


def _packages(img: Image) -> None:
    """Runtime packages, plus toolchains that are removed after the build."""
    img.install(*RUNTIME_PACKAGES).build_install(*BUILD_PACKAGES)


def _boot_init(img: Image) -> None:
    """Boot-time init: TPM-sealed key -> encrypted /persistent -> secrets over HTTP."""
    keys = KeyGeneration()
    key = keys.key("key_persistent", strategy="tpm", output="/tmp/key_persistent")

    disks = DiskEncryption()
    disk = disks.disk(
        "disk_persistent",
        device=None,  # no fixed path: use the largest unpartitioned disk
        key=key,  # reads key.output; check() verifies the key is declared
        mapper_name="cryptroot",
        mount_point="/persistent",
    )

    img.apply(keys, disks, SecretDelivery(method="http_post", store_at=disk))


def _prover_stack(img: Image) -> None:
    """Raiko (prover), Taiko client and Nethermind (execution), all built from source."""
    img.run("mkosi-chroot groupadd -r eth", phase="postinst")  # the modules' users join it
    img.apply(
        Raiko(
            source_repo="https://github.com/NethermindEth/raiko.git",
            source_branch="feat/tdx",
        ),
        TaikoClient(
            source_repo="https://github.com/NethermindEth/surge-taiko-mono",
            source_branch="feat/tdx-proving",
            build_path="packages/taiko-client",
        ),
        Nethermind(
            source_repo="https://github.com/NethermindEth/nethermind.git",
            version="1.32.3",
        ),
    )
    img.run("mkosi-chroot usermod -a -G tdx nethermind-surge", phase="postinst")


def _system_config(img: Image) -> None:
    """Config files: dropbear, sysctl, TDX udev rules, NTP, Prometheus, service env."""
    img.file("/etc/default/dropbear", content=DROPBEAR_CONFIG)
    img.file("/etc/sysctl.d/99-surge.conf", content=SYSCTL_CONF)
    img.file("/etc/udev/rules.d/65-tdx-guest.rules", content=TDX_GUEST_PERMISSIONS)
    img.file("/etc/udev/rules.d/99-tdx-symlink.rules", content=TDX_GUEST_SYMLINK)
    img.file("/etc/openntpd/ntpd.conf", content=OPENNTPD_CONF)
    img.file("/etc/default/prometheus", content=PROMETHEUS_DEFAULTS)
    img.file("/etc/nethermind-surge/env", content=NETHERMIND_ENV)
    img.file("/etc/raiko/env", content=RAIKO_ENV)
    img.file("/etc/taiko-client/env", content=TAIKO_CLIENT_ENV)


def _system_services(img: Image) -> None:
    """Enable packaged daemons; disable and mask OpenSSH so dropbear owns port 22."""
    img.enable("network-setup", "openntpd", "logrotate", "dropbear")
    img.disable("ssh.service", "ssh.socket").mask("ssh.service", "ssh.socket")


def _cloud_profiles(img: Image) -> None:
    """Azure and GCP variants: each adds its platform glue and output target."""
    img.profile("azure").apply(AzurePlatform())
    img.profile("gcp").apply(GcpPlatform())


def _devtools_profile(img: Image) -> None:
    """Debug variant: debugging tools, serial console, root login. Never ship it."""
    img.profile("devtools").apply(Devtools())
