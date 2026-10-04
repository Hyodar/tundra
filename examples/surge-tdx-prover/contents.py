"""File contents the surge-tdx-prover image ships, byte-exact with the committed tree.

Strings keep their trailing newline only where the shipped file has one. Unit bodies
omit the ``After=``/``Requires=runtime-init.service`` that ``after_init`` adds.
``tests/test_surge_contents.py`` checks every constant against ``mkosi/default/``.
"""

from __future__ import annotations

# ── Skeleton (mkosi.skeleton/) ────────────────────────────────────────

TDX_INIT = """\
#!/bin/sh

# Mount essential filesystems
mkdir -p /dev /proc /sys /run
mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev
mount -t tmpfs none /run
mount -t configfs none /sys/kernel/config

# Workaround to make pivot_root work
# https://aconz2.github.io/2024/07/29/container-from-initramfs.html
exec unshare --mount sh -c '
    mkdir /@
    mount --rbind / /@
    cd /@ && mount --move . /
    exec chroot . /lib/systemd/systemd systemd.unit=minimal.target'
"""

RESOLV_CONF = "nameserver 8.8.8.8\nnameserver 8.8.4.4"

NETWORK_SETUP_SERVICE = """\
[Unit]
Description=Basic Network Setup
DefaultDependencies=no
Before=network.target
Wants=network.target

[Service]
Type=oneshot
ExecStart=ip link set lo up
ExecStart=ip link set eth0 up
ExecStart=chattr +i /etc/resolv.conf
ExecStart=/usr/sbin/udhcpc -i eth0 -n
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target"""

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

# ── Service environment files ─────────────────────────────────────────

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

# ── Service units (mkosi.extra/usr/lib/systemd/system/), before after_init ──

RAIKO_UNIT = """\
[Unit]
Description=Raiko
After=tdxs.service
Requires=tdxs.service

[Service]
User=raiko
Group=tdx
Restart=on-failure
ExecStart=/usr/bin/raiko

[Install]
WantedBy=default.target
"""

TAIKO_CLIENT_UNIT = """\
[Unit]
Description=Taiko Client

[Service]
User=taiko-client
Group=eth
Restart=on-failure
ExecStart=/usr/bin/taiko-client

[Install]
WantedBy=default.target
"""

NETHERMIND_UNIT = """\
[Unit]
Description=Nethermind Surge

[Service]
User=nethermind-surge
Group=eth
Restart=on-failure
LimitNOFILE=1048576
EnvironmentFile=/etc/nethermind-surge/env
ExecStart=/usr/bin/nethermind \\
--config /etc/nethermind-surge/config.json \\
--datadir /home/nethermind-surge/data \\
--JsonRpc.EngineHost 0.0.0.0 \\
--JsonRpc.EnginePort 8551

[Install]
WantedBy=default.target
"""
