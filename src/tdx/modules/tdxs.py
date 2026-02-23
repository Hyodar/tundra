"""Built-in TDX quote service (tdxs) module.

Generates config, systemd units, and user/group matching the NethermindEth/tdxs
reference layout used in nethermind-tdx images.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tdx.image import Image


@dataclass(slots=True)
class Tdxs:
    """Configures the tdxs TDX quote issuer/validator service.

    Generates:
      - /etc/tdxs/config.yaml (YAML config)
      - /usr/lib/systemd/system/tdxs.service
      - /usr/lib/systemd/system/tdxs.socket
      - User/group creation and socket enablement via postinst hooks
    """

    issuer_type: str = "dcap"
    socket_path: str = "/var/tdxs.sock"
    user: str = "tdxs"
    group: str = "tdx"
    after: tuple[str, ...] = ("runtime-init.service",)

    def setup(self, image: Image) -> None:
        """Declare build-time package dependencies."""

    def install(self, image: Image) -> None:
        """Apply tdxs runtime configuration to the image."""
        # Config file
        image.file("/etc/tdxs/config.yaml", content=self._render_config())

        # Systemd unit files
        image.file(
            "/usr/lib/systemd/system/tdxs.service",
            content=self._render_service_unit(),
        )
        image.file(
            "/usr/lib/systemd/system/tdxs.socket",
            content=self._render_socket_unit(),
        )

        # Group, user creation, and socket enablement (postinst phase)
        image.run(
            "mkosi-chroot", "groupadd", "--system", self.group,
            phase="postinst",
        )
        image.run(
            "mkosi-chroot", "useradd", "--system",
            "--home-dir", f"/home/{self.user}",
            "--shell", "/usr/sbin/nologin",
            "--gid", self.group,
            self.user,
            phase="postinst",
        )
        image.run(
            "mkosi-chroot", "systemctl", "enable", "tdxs.socket",
            phase="postinst",
        )

    def apply(self, image: Image) -> None:
        """Convenience: call setup() then install()."""
        self.setup(image)
        self.install(image)

    def _render_config(self) -> str:
        """Render /etc/tdxs/config.yaml content."""
        return (
            "transport:\n"
            "  type: socket\n"
            "  config:\n"
            "    systemd: true\n"
            "\n"
            "issuer:\n"
            f"  type: {self.issuer_type}\n"
        )

    def _render_service_unit(self) -> str:
        """Render tdxs.service systemd unit."""
        after_line = " ".join(self.after)
        requires_line = " ".join((*self.after, "tdxs.socket"))
        return (
            "[Unit]\n"
            "Description=TDXS\n"
            f"After={after_line}\n"
            f"Requires={requires_line}\n"
            "\n"
            "[Service]\n"
            f"User={self.user}\n"
            f"Group={self.group}\n"
            f"WorkingDirectory=/home/{self.user}\n"
            "Type=notify\n"
            "ExecStart=/usr/bin/tdxs \\\n"
            "    --config /etc/tdxs/config.yaml\n"
            "Restart=on-failure\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def _render_socket_unit(self) -> str:
        """Render tdxs.socket systemd unit."""
        after_line = " ".join(self.after)
        requires_line = " ".join(self.after)
        return (
            "[Unit]\n"
            "Description=TDXS Socket\n"
            f"After={after_line}\n"
            f"Requires={requires_line}\n"
            "\n"
            "[Socket]\n"
            f"ListenStream={self.socket_path}\n"
            "SocketMode=0660\n"
            "SocketUser=root\n"
            f"SocketGroup={self.group}\n"
            "Accept=false\n"
            "\n"
            "[Install]\n"
            "WantedBy=sockets.target\n"
        )
