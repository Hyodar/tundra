"""Built-in TDX init module (tdx-init + runtime-init).

Generates build pipeline, config, runtime-init script, and systemd unit
matching the NethermindEth/nethermind-tdx init service layout.

Build: clones and compiles the Go binary from source.
Runtime: config.yaml, runtime-init shell script, systemd oneshot service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tdx.image import Image

# Build packages required to compile tdx-init from source
TDX_INIT_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

TDX_INIT_DEFAULT_REPO = "https://github.com/NethermindEth/nethermind-tdx"
TDX_INIT_DEFAULT_BRANCH = "main"


@dataclass(slots=True)
class TdxInit:
    """Configures the tdx-init service and runtime-init script.

    Handles the full lifecycle:
      1. Build: declares build packages (Go, git), adds build hook to clone
         and compile the tdx-init binary from source.
      2. Runtime: generates /etc/tdx-init/config.yaml, /usr/bin/runtime-init
         script, and runtime-init.service systemd unit.
    """

    source_repo: str = TDX_INIT_DEFAULT_REPO
    source_branch: str = TDX_INIT_DEFAULT_BRANCH
    ssh_strategy: str = "webserver"
    key_strategy: str = "tpm"
    disk_strategy: str = "luks"
    mount_point: str = "/persistent"
    runtime_users: tuple[str, ...] = ()
    runtime_directories: tuple[str, ...] = ()
    runtime_devices: tuple[str, ...] = ()

    def setup(self, image: Image) -> None:
        """Declare build-time package dependencies for compiling tdx-init."""
        image.build_install(*TDX_INIT_BUILD_PACKAGES)

    def install(self, image: Image) -> None:
        """Apply tdx-init build hook and runtime configuration to the image."""
        self._add_build_hook(image)
        self._add_runtime_config(image)

    def apply(self, image: Image) -> None:
        """Convenience: call setup() then install()."""
        self.setup(image)
        self.install(image)

    def _add_build_hook(self, image: Image) -> None:
        """Add build phase hook that clones and compiles tdx-init from source."""
        build_cmd = (
            f"TDX_INIT_SRC=$BUILDDIR/tdx-init-src && "
            f"if [ ! -d \"$TDX_INIT_SRC\" ]; then "
            f"git clone --depth=1 -b {self.source_branch} "
            f"{self.source_repo} \"$TDX_INIT_SRC\"; "
            f"fi && "
            f"cd \"$TDX_INIT_SRC/init\" && "
            f"GOCACHE=$BUILDDIR/go-cache "
            f'go build -trimpath -ldflags "-s -w -buildid=" '
            f"-o ./build/tdx-init ./cmd/main.go && "
            f"install -m 0755 ./build/tdx-init \"$DESTDIR/usr/bin/tdx-init\""
        )
        image.hook("build", "sh", "-c", build_cmd, shell=True)

    def _add_runtime_config(self, image: Image) -> None:
        """Add runtime config, unit files, and runtime-init script."""
        # Config file
        image.file("/etc/tdx-init/config.yaml", content=self._render_config())

        # Runtime-init script
        image.file(
            "/usr/bin/runtime-init",
            content=self._render_runtime_init_script(),
            mode="0755",
        )

        # Systemd unit file
        image.file(
            "/usr/lib/systemd/system/runtime-init.service",
            content=self._render_service_unit(),
        )

        # Enable the service
        image.run(
            "mkosi-chroot", "systemctl", "enable", "runtime-init.service",
            phase="postinst",
        )

    def _render_config(self) -> str:
        """Render /etc/tdx-init/config.yaml content."""
        return (
            "ssh:\n"
            f"  strategy: {self.ssh_strategy}\n"
            "\n"
            "key:\n"
            f"  strategy: {self.key_strategy}\n"
            "\n"
            "disk:\n"
            f"  strategy: {self.disk_strategy}\n"
            f"  mount_point: {self.mount_point}\n"
        )

    def _render_runtime_init_script(self) -> str:
        """Render /usr/bin/runtime-init shell script."""
        lines = [
            "#!/bin/bash",
            "set -euo pipefail",
            "",
        ]

        # Group/user existence checks
        for user in self.runtime_users:
            lines.append(f'if ! getent group "{user}" >/dev/null 2>&1; then')
            lines.append(f'    echo "Warning: group {user} does not exist"')
            lines.append("fi")
            lines.append(f'if ! getent passwd "{user}" >/dev/null 2>&1; then')
            lines.append(f'    echo "Warning: user {user} does not exist"')
            lines.append("fi")

        # /persistent mount check
        lines.append("")
        lines.append(f'if ! mountpoint -q "{self.mount_point}"; then')
        lines.append(f'    echo "Warning: {self.mount_point} is not mounted"')
        lines.append("fi")

        # JWT generation
        lines.append("")
        lines.append("# Generate JWT secret if not exists")
        lines.append(f'JWT_DIR="{self.mount_point}/jwt"')
        lines.append('mkdir -p "$JWT_DIR"')
        lines.append('if [ ! -f "$JWT_DIR/jwt.hex" ]; then')
        lines.append('    openssl rand -hex 32 > "$JWT_DIR/jwt.hex"')
        lines.append("fi")

        # Directory creation with ownership
        for directory in self.runtime_directories:
            lines.append(f'mkdir -p "{directory}"')

        # TPM/TDX device permission checks
        for device in self.runtime_devices:
            lines.append("")
            lines.append(f'if [ ! -e "{device}" ]; then')
            lines.append(f'    echo "Warning: device {device} not found"')
            lines.append("fi")

        lines.append("")
        return "\n".join(lines)

    def _render_service_unit(self) -> str:
        """Render runtime-init.service systemd unit."""
        return (
            "[Unit]\n"
            "Description=Runtime Init\n"
            "After=network.target network-setup.service\n"
            "\n"
            "[Service]\n"
            "Type=oneshot\n"
            "ExecStart=/usr/bin/tdx-init setup /etc/tdx-init/config.yaml\n"
            "ExecStartPost=/usr/bin/runtime-init\n"
            "RemainAfterExit=yes\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
