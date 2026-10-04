"""Built-in Taiko Client service module.

Generates build pipeline, systemd unit, and user/group matching the
NethermindEth/surge-taiko-mono reference layout used in nethermind-tdx images.

Build: clones and compiles the Go binary from source with CGO flags.
Runtime: systemd service, user creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tundravm.modules.base import Module
from tundravm.modules.resolve import resolve_after
from tundravm.source import GitSource, GoBuild, Install, SourceBuild

if TYPE_CHECKING:
    from tundravm.image import Image

# Build packages required to compile taiko-client from source
TAIKO_CLIENT_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

# CGO flags for the portable blst build
TAIKO_CLIENT_GO_ENV = {
    "GO111MODULE": "on",
    "CGO_CFLAGS": "-O -D__BLST_PORTABLE__",
    "CGO_CFLAGS_ALLOW": "-O -D__BLST_PORTABLE__",
}

TAIKO_CLIENT_DEFAULT_REPO = "https://github.com/NethermindEth/surge-taiko-mono"
TAIKO_CLIENT_DEFAULT_BRANCH = "feat/tdx-proving"
TAIKO_CLIENT_DEFAULT_BUILD_PATH = "packages/taiko-client"


@dataclass(slots=True)
class TaikoClient(Module):
    """Configures the Taiko Client service.

    Handles the full lifecycle:
      1. Build: declares build packages (Go, git), adds build hook to clone
         and compile the taiko-client binary from source with CGO flags.
      2. Runtime: generates systemd service unit and creates system user.
    """

    source: GitSource = GitSource(
        TAIKO_CLIENT_DEFAULT_REPO,
        TAIKO_CLIENT_DEFAULT_BRANCH,
        subdir=TAIKO_CLIENT_DEFAULT_BUILD_PATH,
    )
    user: str = "taiko-client"
    group: str = "eth"
    after: tuple[str, ...] = ()

    def configure(self, image: Image) -> None:
        """Build taiko-client, then declare its unit file and service user."""
        image.build_packages(*TAIKO_CLIENT_BUILD_PACKAGES)
        image.build_from(self.source_spec())
        self._add_runtime_config(image)

    def source_spec(self) -> SourceBuild:
        """The taiko-client source build: ``go build`` of ``cmd/main.go`` in ``source.subdir``.

        The toolchain comes from the image's own build packages (``packages=()``);
        ``output_dir``/``mkdir``, ``cache_key`` and ``mark_unpinned`` keep the unpinned
        hook byte-identical to the hand-written one this module emitted before
        source builds existed.
        """
        return SourceBuild(
            name="taiko-client",
            source=self.source,
            build=GoBuild(
                output="taiko-client",
                package="cmd/main.go",
                output_dir="bin",
                mkdir=False,
                env=TAIKO_CLIENT_GO_ENV,
                packages=(),
            ),
            install=(Install.artifact("/usr/bin/taiko-client"),),
            cache_key=f"taiko-client-{self.source.ref}",
            mark_unpinned=False,
        )

    def _resolve_after(self, image: Image) -> tuple[str, ...]:
        return resolve_after(self.after, image)

    def _add_runtime_config(self, image: Image) -> None:
        """Add runtime config, unit file, and user creation."""
        resolved_after = self._resolve_after(image)
        image.file(
            "/usr/lib/systemd/system/taiko-client.service",
            content=self._render_service_unit(after=resolved_after),
        )

        image.shell(
            f"mkosi-chroot useradd --system --home-dir /home/{self.user} "
            f"--shell /usr/sbin/nologin --groups {self.group} {self.user}",
            phase="postinst",
        )
        image.enable("taiko-client")

    def _render_service_unit(self, *, after: tuple[str, ...] | None = None) -> str:
        """Render taiko-client.service systemd unit."""
        effective = after if after is not None else self.after
        lines = ["[Unit]", "Description=Taiko Client"]
        if effective:
            lines.append(f"After={' '.join(effective)}")
            lines.append(f"Requires={' '.join(effective)}")
        lines.append("")
        lines.extend(
            [
                "[Service]",
                f"User={self.user}",
                f"Group={self.group}",
                "Restart=on-failure",
                "ExecStart=/usr/bin/taiko-client",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )
        return "\n".join(lines)
