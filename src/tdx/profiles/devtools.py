"""Devtools platform profile helper.

Adds debugging packages, serial console access, and password-based root login
to an Image within an ``img.profile("devtools")`` context.  This profile is
intended for development/debugging and should **not** be used in production.
"""

from __future__ import annotations

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


def apply_devtools_profile(image: Image) -> None:
    """Populate the active devtools profile on *image*.

    Must be called inside an ``img.profile("devtools")`` context::

        with img.profile("devtools"):
            apply_devtools_profile(img)

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
        "mkosi-chroot", "systemctl", "enable",
        "serial-console.service",
        phase="postinst",
    )

    # Root password + auth configuration
    image.run(
        "bash", "-c", DEVTOOLS_POSTINST_SCRIPT,
        phase="postinst",
    )
