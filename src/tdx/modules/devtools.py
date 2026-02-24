"""Built-in Devtools module.

Adds debugging packages, serial console access, and password-based root login
to an Image.  This module is intended for development/debugging and should
**not** be used in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tdx.image import Image

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


@dataclass(slots=True)
class Devtools:
    """Devtools module for development and debugging.

    Adds:
    * Debug/diagnostic runtime packages (bash-completion, curl, vim, etc.)
    * ``serial-console.service`` enabling serial-getty on ttyS0
    * PostInst hook setting root password and enabling password auth for
      dropbear and openssh

    .. note::

        To preserve ``/usr/share/bash-completion`` when debloat is active,
        pass ``paths_skip_for_profiles={"devtools": ("/usr/share/bash-completion",)}``
        to :meth:`Image.debloat`.
    """

    def setup(self, image: Image) -> None:
        """No build-time dependencies for devtools."""

    def install(self, image: Image) -> None:
        """Apply devtools configuration to the image."""
        # Debug runtime packages
        for pkg in DEVTOOLS_PACKAGES:
            image.install(pkg)

        # Serial console service
        image.file(
            "/usr/lib/systemd/system/serial-console.service",
            content=SERIAL_CONSOLE_SERVICE,
        )

        # Enable serial-console service
        image.run(
            "mkosi-chroot",
            "systemctl",
            "enable",
            "serial-console.service",
            phase="postinst",
        )

        # Root password + auth configuration
        image.run(DEVTOOLS_POSTINST_SCRIPT, phase="postinst", shell=True)

    def apply(self, image: Image) -> None:
        """Convenience: call setup() then install()."""
        self.setup(image)
        self.install(image)
