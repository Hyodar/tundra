"""Built-in Taiko Client service module.

Generates build pipeline, systemd unit, and user/group matching the
NethermindEth/surge-taiko-mono reference layout used in nethermind-tdx images.

Build: clones and compiles the Go binary from source with CGO flags.
Runtime: systemd service, user creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from textwrap import dedent
from typing import TYPE_CHECKING

from tundravm.build_cache import Build, Cache
from tundravm.modules.resolve import resolve_after

if TYPE_CHECKING:
    from tundravm.image import Image

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
    after: tuple[str, ...] = ()

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
        clone_dir = Build.build_path("taiko-client")
        chroot_dir = Build.chroot_path("taiko-client")
        cache = Cache.declare(
            f"taiko-client-{self.source_branch}",
            (
                Cache.file(
                    src=Build.build_path(f"taiko-client/{self.build_path}/bin/taiko-client"),
                    dest=Build.dest_path("usr/bin/taiko-client"),
                    name="taiko-client",
                ),
            ),
        )

        build_cmd = (
            f"git clone --depth=1 -b {self.source_branch} "
            f'{self.source_repo} "{clone_dir}" && '
            "mkosi-chroot bash -c '"
            f"cd {chroot_dir}/{self.build_path} && "
            'GO111MODULE=on CGO_CFLAGS="-O -D__BLST_PORTABLE__" '
            'CGO_CFLAGS_ALLOW="-O -D__BLST_PORTABLE__" '
            'go build -trimpath -ldflags "-s -w -buildid=" '
            "-o bin/taiko-client cmd/main.go"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd))

    def _resolve_after(self, image: Image) -> tuple[str, ...]:
        return resolve_after(self.after, image)

    def _add_runtime_config(self, image: Image) -> None:
        """Add runtime config, unit file, and user creation."""
        resolved_after = self._resolve_after(image)
        image.file(
            "/usr/lib/systemd/system/taiko-client.service",
            content=self._render_service_unit(after=resolved_after),
        )

        image.run(
            f"mkosi-chroot useradd --system --home-dir /home/{self.user} "
            f"--shell /usr/sbin/nologin --groups {self.group} {self.user}",
            phase="postinst",
        )
        image.service("taiko-client", enabled=True)

    def _render_service_unit(self, *, after: tuple[str, ...] | None = None) -> str:
        """Render taiko-client.service systemd unit."""
        effective = after if after is not None else self.after
        after_line = " ".join(effective)
        requires_line = " ".join(effective)
        return dedent(f"""\
            [Unit]
            Description=Taiko Client
            After={after_line}
            Requires={requires_line}

            [Service]
            User={self.user}
            Group={self.group}
            Restart=on-failure
            ExecStart=/usr/bin/taiko-client

            [Install]
            WantedBy=default.target
        """)
