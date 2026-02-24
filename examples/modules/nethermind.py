"""Built-in Nethermind service module.

Generates build pipeline, systemd unit, and user/group matching the
NethermindEth/nethermind reference layout used in nethermind-tdx images.

Build: clones and compiles the .NET execution client from source with
deterministic publish properties.
Runtime: systemd service, user creation, config file mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from textwrap import dedent
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tdx.image import Image

# Build packages required to compile nethermind from source
NETHERMIND_BUILD_PACKAGES = (
    "dotnet-sdk-10.0",
    "dotnet-runtime-10.0",
    "build-essential",
    "git",
)

NETHERMIND_DEFAULT_REPO = "https://github.com/NethermindEth/nethermind.git"
NETHERMIND_DEFAULT_PROJECT = "src/Nethermind/Nethermind.Runner"
NETHERMIND_DEFAULT_RUNTIME = "linux-x64"


@dataclass(slots=True)
class Nethermind:
    """Configures the Nethermind .NET execution client service.

    Handles the full lifecycle:
      1. Build: declares build packages, adds build hook to clone
         and compile the Nethermind binary from source with deterministic
         publish properties.
      2. Runtime: generates systemd service unit, creates system user,
         and installs config files.
    """

    source_repo: str = NETHERMIND_DEFAULT_REPO
    version: str = "1.32.3"
    project_path: str = NETHERMIND_DEFAULT_PROJECT
    runtime: str = NETHERMIND_DEFAULT_RUNTIME
    config_files: dict[str, str] = field(default_factory=dict)
    user: str = "nethermind-surge"
    group: str = "eth"
    after: tuple[str, ...] = ()

    def setup(self, image: Image) -> None:
        """Declare build-time package dependencies for compiling nethermind."""
        image.build_install(*NETHERMIND_BUILD_PACKAGES)

    def install(self, image: Image) -> None:
        """Apply nethermind build hook and runtime configuration to the image."""
        self._add_build_hook(image)
        self._add_runtime_config(image)

    def apply(self, image: Image) -> None:
        """Convenience: call setup() then install()."""
        self.setup(image)
        self.install(image)

    def _add_build_hook(self, image: Image) -> None:
        """Add build phase hook that clones and compiles nethermind from source."""
        build_cmd = (
            f"NETHERMIND_SRC=$BUILDDIR/nethermind-src && "
            f'if [ ! -d "$NETHERMIND_SRC" ]; then '
            f"git clone --depth=1 -b {self.version} "
            f'{self.source_repo} "$NETHERMIND_SRC"; '
            f"fi && "
            f'cd "$NETHERMIND_SRC" && '
            f"DOTNET_CLI_TELEMETRY_OPTOUT=1 "
            f"dotnet publish {self.project_path} "
            f"-c Release "
            f"-r {self.runtime} "
            f'-o "$BUILDDIR/nethermind-out" '
            f"/p:Deterministic=true "
            f"/p:ContinuousIntegrationBuild=true "
            f"/p:PublishSingleFile=true "
            f"/p:BuildTimestamp=0 "
            f"/p:Commit=0000000000000000000000000000000000000000 && "
            f'install -m 0755 "$BUILDDIR/nethermind-out/nethermind" '
            f'"$DESTDIR/usr/bin/nethermind" && '
            f'install -d "$DESTDIR/etc/nethermind-surge" && '
            f'if [ -f "$BUILDDIR/nethermind-out/NLog.config" ]; then '
            f'install -m 0644 "$BUILDDIR/nethermind-out/NLog.config" '
            f'"$DESTDIR/etc/nethermind-surge/NLog.config"; '
            f"fi && "
            f'if [ -d "$BUILDDIR/nethermind-out/plugins" ]; then '
            f'cp -r "$BUILDDIR/nethermind-out/plugins" '
            f'"$DESTDIR/etc/nethermind-surge/plugins"; '
            f"fi"
        )
        image.hook("build", "sh", "-c", build_cmd, shell=True)

    def _resolve_after(self, image: Image) -> tuple[str, ...]:
        """Build the After= list, prepending the init service if available."""
        after = list(self.after)
        if image.init is not None and image.init.has_scripts:
            init_svc = image.init.service_name
            if init_svc not in after:
                after.insert(0, init_svc)
        return tuple(after)

    def _add_runtime_config(self, image: Image) -> None:
        """Add runtime config, unit file, and user creation."""
        resolved_after = self._resolve_after(image)
        image.file(
            "/usr/lib/systemd/system/nethermind-surge.service",
            content=self._render_service_unit(after=resolved_after),
        )

        for src_path, dest_path in self.config_files.items():
            image.file(dest_path, src=src_path)

        image.run(
            "mkosi-chroot",
            "useradd",
            "--system",
            "--home-dir",
            f"/home/{self.user}",
            "--shell",
            "/usr/sbin/nologin",
            "--groups",
            self.group,
            self.user,
            phase="postinst",
        )

    def _render_service_unit(self, *, after: tuple[str, ...] | None = None) -> str:
        """Render nethermind-surge.service systemd unit."""
        effective = after if after is not None else self.after
        after_line = " ".join(effective)
        requires_line = " ".join(effective)
        return dedent(f"""\
            [Unit]
            Description=Nethermind Surge
            After={after_line}
            Requires={requires_line}

            [Service]
            User={self.user}
            Group={self.group}
            Restart=on-failure
            LimitNOFILE=1048576
            EnvironmentFile=/etc/nethermind-surge/env
            ExecStart=/usr/bin/nethermind \
            --config /etc/nethermind-surge/config.json \
            --datadir /home/nethermind-surge/data \
            --JsonRpc.EngineHost 0.0.0.0 \
            --JsonRpc.EnginePort 8551

            [Install]
            WantedBy=default.target
        """)
