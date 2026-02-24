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

from tdx.build_cache import Build, Cache

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
        clone_dir = Build.build_path("nethermind")
        chroot_dir = Build.chroot_path("nethermind")
        svc = self.user  # service name for dest paths

        cache = Cache.declare(
            f"nethermind-{self.version}-{self.runtime}",
            (
                Cache.file(
                    src=Build.build_path("nethermind/publish/nethermind"),
                    dest=Build.dest_path("usr/bin/nethermind"),
                    name="nethermind",
                ),
                Cache.file(
                    src=Build.build_path("nethermind/publish/NLog.config"),
                    dest=Build.dest_path(f"etc/{svc}/NLog.config"),
                    name="NLog.config",
                    mode="0644",
                ),
                Cache.dir(
                    src=Build.build_path("nethermind/publish/plugins"),
                    dest=Build.dest_path(f"etc/{svc}/plugins"),
                    name="plugins",
                ),
            ),
        )

        build_cmd = (
            f"git clone --depth=1 -b {self.version} "
            f'{self.source_repo} "{clone_dir}" && '
            "mkosi-chroot bash -c '"
            "export "
            "DOTNET_CLI_TELEMETRY_OPTOUT=1 "
            "DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 "
            "DOTNET_NOLOGO=1 "
            "DOTNET_CLI_HOME=/tmp/dotnet "
            "NUGET_PACKAGES=/tmp/nuget "
            f"&& cd {chroot_dir} "
            f"&& dotnet restore {self.project_path} "
            f"--runtime {self.runtime} "
            "--disable-parallel "
            "--force "
            f"&& dotnet publish {self.project_path} "
            f"--configuration Release "
            f"--runtime {self.runtime} "
            "--self-contained true "
            f"--output {chroot_dir}/publish "
            "-p:Deterministic=true "
            "-p:ContinuousIntegrationBuild=true "
            "-p:PublishSingleFile=true "
            "-p:BuildTimestamp=0 "
            "-p:Commit=0000000000000000000000000000000000000000 "
            "-p:PublishReadyToRun=false "
            "-p:DebugType=none "
            "-p:IncludeAllContentForSelfExtract=true "
            "-p:IncludePackageReferencesDuringMarkupCompilation=true "
            "-p:EmbedUntrackedSources=true "
            "-p:PublishRepositoryUrl=true"
            "'"
        )
        image.hook("build", "sh", "-c", cache.wrap(build_cmd), shell=True)

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
