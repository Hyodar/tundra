"""Packages, serial console unit and login script of the ``declarative.utils.DevTools`` fragment.

Development/debugging only: it enables password-based root login. The root password is
hashed with a fixed salt (:data:`DERIVED_SALT_LINE`) so that two builds of the variant
write the same ``/etc/shadow``; :data:`DEVTOOLS_POSTINST_SCRIPT` is the historical script,
whose ``openssl passwd`` picks a random salt on every build.
"""

from __future__ import annotations

from tundravm._units import v1

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

SERIAL_CONSOLE_SERVICE = v1.SERIAL_CONSOLE

# ---------------------------------------------------------------------------
# PostInst script — root password, dropbear/openssh auth configuration
# ---------------------------------------------------------------------------

HISTORICAL_HASH_LINE = 'ROOT_PASS=$(openssl passwd -6 "tdx")'
"""The line of :data:`DEVTOOLS_POSTINST_SCRIPT` that hashes the password with a random salt."""

DERIVED_SALT_LINE = (
    "SALT=$(printf 'tundravm-devtools:%s' \"${IMAGE_ID:-devtools}\" | sha256sum | cut -c1-16)"
)
"""The default salt: the first 16 hex digits of ``sha256("tundravm-devtools:<IMAGE_ID>")``,
where mkosi's ``$IMAGE_ID`` is the variant's name (tundravm sets ``ImageId=`` and
``--image-id`` to it), so it is fixed per variant."""

SALTED_HASH_LINE = 'ROOT_PASS=$(openssl passwd -6 -salt "$SALT" "tdx")'
"""The line that hashes the password with ``$SALT``, which the line before it sets."""

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
