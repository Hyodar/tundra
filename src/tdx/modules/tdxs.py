"""Built-in TDX quote service (tdxs) module.

Generates build pipeline, config, systemd units, and user/group matching the
NethermindEth/tdxs reference layout used in nethermind-tdx images.

Build: clones and compiles the Go binary from source.
Runtime: config.yaml, systemd service + socket activation, user/group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tdx.image import Image

# Build packages required to compile tdxs from source
TDXS_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

TDXS_DEFAULT_REPO = "https://github.com/NethermindEth/tdxs"
TDXS_DEFAULT_BRANCH = "master"


@dataclass(slots=True)
class Tdxs:
    """Configures the tdxs TDX quote issuer/validator service.

    Handles the full lifecycle:
      1. Build: declares build packages (Go, git), adds build hook to clone
         and compile the tdxs binary from source.
      2. Runtime: generates /etc/tdxs/config.yaml, systemd service + socket
         units, user/group creation, and socket enablement.
    """

    issuer_type: str = "dcap"
    socket_path: str = "/var/tdxs.sock"
    user: str = "tdxs"
    group: str = "tdx"
    after: tuple[str, ...] = ("runtime-init.service",)
    source_repo: str = TDXS_DEFAULT_REPO
    source_branch: str = TDXS_DEFAULT_BRANCH

    def setup(self, image: Image) -> None:
        """Declare build-time package dependencies for compiling tdxs."""
        image.build_install(*TDXS_BUILD_PACKAGES)

    def install(self, image: Image) -> None:
        """Apply tdxs build hook and runtime configuration to the image."""
        self._add_build_hook(image)
        self._add_runtime_config(image)

    def apply(self, image: Image) -> None:
        """Convenience: call setup() then install()."""
        self.setup(image)
        self.install(image)

    def _add_build_hook(self, image: Image) -> None:
        """Add build phase hook that clones and compiles tdxs from source."""
        build_cmd = (
            f"TDXS_SRC=$BUILDDIR/tdxs-src && "
            f"if [ ! -d \"$TDXS_SRC\" ]; then "
            f"git clone --depth=1 -b {self.source_branch} "
            f"{self.source_repo} \"$TDXS_SRC\"; "
            f"fi && "
            f"cd \"$TDXS_SRC\" && "
            f"make sync-constellation && "
            f"GOCACHE=$BUILDDIR/go-cache "
            f'go build -trimpath -ldflags "-s -w -buildid=" '
            f"-o \"$DESTDIR/usr/bin/tdxs\" ./cmd/tdxs/main.go"
        )
        image.hook("build", "sh", "-c", build_cmd, shell=True)

    def _add_runtime_config(self, image: Image) -> None:
        """Add runtime config, unit files, user/group, and service enablement."""
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
