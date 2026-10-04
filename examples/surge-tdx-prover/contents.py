"""File contents the surge-tdx-prover image ships, byte-exact with the committed tree.

Strings keep their trailing newline only where the shipped file has one. Unit bodies
omit the ``After=``/``Requires=runtime-init.service`` that ``after_init`` adds.
The base layer and the service fragments own their files; they are re-exported
here so ``tests/test_surge_contents.py`` checks every constant against ``mkosi/default/``.
"""

from __future__ import annotations

from examples.fragments.nethermind import NETHERMIND_ENV, NETHERMIND_UNIT
from examples.fragments.raiko import RAIKO_ENV, RAIKO_UNIT
from examples.fragments.taiko_client import TAIKO_CLIENT_ENV, TAIKO_CLIENT_UNIT
from examples.nethermind_tdx import NETWORK_SETUP_SERVICE, RESOLV_CONF, TDX_INIT

# ── System configuration (mkosi.extra/etc/) ───────────────────────────

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

__all__ = [
    "DROPBEAR_CONFIG",
    "NETHERMIND_ENV",
    "NETHERMIND_UNIT",
    "NETWORK_SETUP_SERVICE",
    "OPENNTPD_CONF",
    "PROMETHEUS_DEFAULTS",
    "RAIKO_ENV",
    "RAIKO_UNIT",
    "RESOLV_CONF",
    "SYSCTL_CONF",
    "TAIKO_CLIENT_ENV",
    "TAIKO_CLIENT_UNIT",
    "TDX_GUEST_PERMISSIONS",
    "TDX_GUEST_SYMLINK",
    "TDX_INIT",
]
