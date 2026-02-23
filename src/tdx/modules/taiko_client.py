"""Built-in Taiko Client service module.

Generates build pipeline, systemd unit, and user/group matching the
NethermindEth/surge-taiko-mono reference layout used in nethermind-tdx images.

Build: clones and compiles the Go binary from source with CGO flags.
Runtime: systemd service, user creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tdx.image import Image

# Build packages required to compile taiko-client from source
TAIKO_CLIENT_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

TAIKO_CLIENT_DEFAULT_REPO = "https://github.com/NethermindEth/surge-taiko-mono"
TAIKO_CLIENT_DEFAULT_BRANCH = "feat/tdx-proving"
TAIKO_CLIENT_DEFAULT_BUILD_PATH = "packages/taiko-client"


@dataclass(slots=True)
class TaikoClient:
    """Configures the Taiko Client service.

    Handles the full lifecycle:
      1. Build: declares build packages (Go, git), adds build hook to clone
         and compile the taiko-client binary from source with CGO flags.
      2. Runtime: generates systemd service unit and creates system user.
    """

    source_repo: str = TAIKO_CLIENT_DEFAULT_REPO
    source_branch: str = TAIKO_CLIENT_DEFAULT_BRANCH
    build_path: str = TAIKO_CLIENT_DEFAULT_BUILD_PATH
    user: str = "taiko-client"
    group: str = "eth"
    after: tuple[str, ...] = ("runtime-init.service",)

    def setup(self, image: Image) -> None:
        """Declare build-time package dependencies for compiling taiko-client."""
        image.build_install(*TAIKO_CLIENT_BUILD_PACKAGES)

    def install(self, image: Image) -> None:
        """Apply taiko-client build hook and runtime configuration to the image."""
        self._add_build_hook(image)
        self._add_runtime_config(image)

    def apply(self, image: Image) -> None:
        """Convenience: call setup() then install()."""
        self.setup(image)
        self.install(image)

    def _add_build_hook(self, image: Image) -> None:
        """Add build phase hook that clones and compiles taiko-client from source."""
        build_cmd = (
            f"TAIKO_SRC=$BUILDDIR/taiko-client-src && "
            f"if [ ! -d \"$TAIKO_SRC\" ]; then "
            f"git clone --depth=1 -b {self.source_branch} "
            f"{self.source_repo} \"$TAIKO_SRC\"; "
            f"fi && "
            f"cd \"$TAIKO_SRC/{self.build_path}\" && "
            f"GOCACHE=$BUILDDIR/go-cache "
            f'CGO_CFLAGS="-O -D__BLST_PORTABLE__" '
            f'CGO_CFLAGS_ALLOW="-O -D__BLST_PORTABLE__" '
            f'go build -trimpath -ldflags "-s -w -buildid=" '
            f"-o ./build/taiko-client . && "
            f"install -m 0755 ./build/taiko-client \"$DESTDIR/usr/bin/taiko-client\""
        )
        image.hook("build", "sh", "-c", build_cmd, shell=True)

    def _add_runtime_config(self, image: Image) -> None:
        """Add runtime config, unit file, and user creation."""
        # Systemd unit file
        image.file(
            "/usr/lib/systemd/system/taiko-client.service",
            content=self._render_service_unit(),
        )

        # User creation (postinst phase)
        image.run(
            "mkosi-chroot", "useradd", "--system",
            "--home-dir", f"/home/{self.user}",
            "--shell", "/usr/sbin/nologin",
            "--groups", self.group,
            self.user,
            phase="postinst",
        )

    def _render_service_unit(self) -> str:
        """Render taiko-client.service systemd unit."""
        after_line = " ".join(self.after)
        requires_line = " ".join(self.after)
        return (
            "[Unit]\n"
            "Description=Taiko Client\n"
            f"After={after_line}\n"
            f"Requires={requires_line}\n"
            "\n"
            "[Service]\n"
            f"User={self.user}\n"
            f"Group={self.group}\n"
            "Restart=on-failure\n"
            "ExecStart=/usr/bin/taiko-client\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
