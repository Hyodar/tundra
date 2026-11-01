"""Packages, serial console unit and login script of the ``declarative.utils.DevTools`` fragment.

Development/debugging only: it enables password-based root login. The root password is
hashed with a fixed salt (:data:`DERIVED_SALT_LINE`) so that two builds of the variant
write the same ``/etc/shadow``; :data:`DEVTOOLS_POSTINST_SCRIPT` is the historical script,
whose ``openssl passwd`` picks a random salt on every build. The password is a console
login unless :data:`NETWORK_LOGIN_SCRIPT` opens SSH to it as well.
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

SERIAL_GETTY = "serial-getty@ttyS0.service"
"""The getty ``DevTools`` enables at build time (systemd's ``serial-getty@.service``)."""

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

SET_PASSWORD_LINE = "printf 'root:%s\\n' \"$ROOT_PASS\" | mkosi-chroot chpasswd -e"
"""The line that writes the hash into the image's ``/etc/shadow``. A postinst script runs
outside the image with the tools tree's ``/etc`` bound read-only, so only ``mkosi-chroot``
reaches the image's account files; the hash replaces the whole field, which unlocks root."""

DEVTOOLS_POSTINST_SCRIPT = f"""\
# Set root password and unlock account
ROOT_PASS=$(openssl passwd -6 "tdx")
{SET_PASSWORD_LINE}
"""

NETWORK_LOGIN_SCRIPT = """
# Enable password authentication for dropbear (remove restrictive flags)
if [ -f "$BUILDROOT/etc/default/dropbear" ]; then
    sed -i 's/ -s//g; s/ -w//g; s/ -g//g' "$BUILDROOT/etc/default/dropbear"
fi

# Enable password authentication for openssh
mkdir -p "$BUILDROOT/etc/ssh/sshd_config.d"
cat > "$BUILDROOT/etc/ssh/sshd_config.d/99-devtools.conf" << 'SSHEOF'
 PermitRootLogin yes
 PasswordAuthentication yes
SSHEOF
"""
"""What ``DevTools(network_login=True)`` appends: root logs in with the password over SSH."""
