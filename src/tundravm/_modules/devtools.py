"""Packages, serial console unit and login script of the ``declarative.utils.DevTools`` fragment.

Development/debugging only: it enables password-based root login.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Debug packages installed in the devtools profile
# ---------------------------------------------------------------------------

DEVTOOLS_PACKAGES: tuple[str, ...] = (
    "apt",
    "bash-completion",
    "curl",
    "dnsutils",
    "iputils-ping",
    "net-tools",
    "netcat-openbsd",
    "openssh-server",
    "socat",
    "strace",
    "tcpdump",
    "tcpflow",
    "vim",
)

# ---------------------------------------------------------------------------
# serial-console.service — enables serial getty on ttyS0
# ---------------------------------------------------------------------------

SERIAL_CONSOLE_SERVICE = """\
[Unit]
Description=Serial Console
After=systemd-logind.service

[Service]
Type=oneshot
ExecStart=/bin/systemctl enable serial-getty@ttyS0.service
ExecStartPost=/bin/systemctl start serial-getty@ttyS0.service
RemainAfterExit=yes

[Install]
WantedBy=minimal.target
"""

# ---------------------------------------------------------------------------
# PostInst script — root password, dropbear/openssh auth configuration
# ---------------------------------------------------------------------------

DEVTOOLS_POSTINST_SCRIPT = """\
# Set root password and unlock account
ROOT_PASS=$(openssl passwd -6 "tdx")
usermod -p "$ROOT_PASS" root
passwd -u root

# Enable password authentication for dropbear (remove restrictive flags)
if [ -f /etc/default/dropbear ]; then
    sed -i 's/ -s//g; s/ -w//g; s/ -g//g' /etc/default/dropbear
fi

# Enable password authentication for openssh
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-devtools.conf << 'SSHEOF'
 PermitRootLogin yes
 PasswordAuthentication yes
SSHEOF
"""
