"""Built-in Nethermind service module.

Generates build pipeline, systemd unit, and user/group matching the
NethermindEth/nethermind reference layout used in nethermind-tdx images.

Build: clones and compiles the .NET execution client from source with
deterministic publish properties.
Runtime: systemd service, user creation, config file mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tundravm.modules.base import Module
from tundravm.modules.resolve import resolve_after
from tundravm.source import DotnetBuild, GitSource, Install, SourceBuild

if TYPE_CHECKING:
    from tundravm.image import Image

# Build packages required to compile nethermind from source
NETHERMIND_BUILD_PACKAGES = (
    "dotnet-sdk-10.0",
    "dotnet-runtime-10.0",
    "build-essential",
    "git",
)

# Keep the dotnet CLI quiet and its state out of the source tree
NETHERMIND_DOTNET_ENV = {
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "DOTNET_NOLOGO": "1",
    "DOTNET_CLI_HOME": "/tmp/dotnet",
    "NUGET_PACKAGES": "/tmp/nuget",
}

# Reproducibility properties for the single-file publish
NETHERMIND_PUBLISH_PROPERTIES = {
    "PublishSingleFile": "true",
    "BuildTimestamp": "0",
    "Commit": "0" * 40,
    "PublishReadyToRun": "false",
    "DebugType": "none",
    "IncludeAllContentForSelfExtract": "true",
    "IncludePackageReferencesDuringMarkupCompilation": "true",
    "EmbedUntrackedSources": "true",
    "PublishRepositoryUrl": "true",
}

NETHERMIND_DEFAULT_REPO = "https://github.com/NethermindEth/nethermind.git"
NETHERMIND_DEFAULT_VERSION = "1.32.3"
NETHERMIND_DEFAULT_PROJECT = "src/Nethermind/Nethermind.Runner"
NETHERMIND_DEFAULT_RUNTIME = "linux-x64"


@dataclass(slots=True)
class Nethermind(Module):
    """Configures the Nethermind .NET execution client service.

    Handles the full lifecycle:
      1. Build: declares build packages, adds build hook to clone
         and compile the Nethermind binary from source with deterministic
         publish properties.
      2. Runtime: generates systemd service unit, creates system user,
         and installs config files.
    """

    source: GitSource = GitSource(NETHERMIND_DEFAULT_REPO, NETHERMIND_DEFAULT_VERSION)
    project_path: str = NETHERMIND_DEFAULT_PROJECT
    runtime: str = NETHERMIND_DEFAULT_RUNTIME
    config_files: dict[str, str] = field(default_factory=dict)
    user: str = "nethermind-surge"
    group: str = "eth"
    after: tuple[str, ...] = ()

    @property
    def version(self) -> str:
        """The release built: the source ref (a tag such as ``1.32.3``)."""
        return self.source.ref

    def configure(self, image: Image) -> None:
        """Build nethermind, then declare its unit file, config files and user."""
        image.build_packages(*NETHERMIND_BUILD_PACKAGES)
        image.build_from(self.source_spec())
        self._add_runtime_config(image)

    def source_spec(self) -> SourceBuild:
        """The nethermind source build: ``dotnet publish`` of ``project_path`` at ``version``.

        Installs the runner binary, its ``NLog.config`` and the ``plugins`` directory
        under ``/etc/<user>``. The toolchain comes from the image's own build
        packages (``packages=()``); ``cache_key`` and ``mark_unpinned`` keep the
        unpinned hook byte-identical to the hand-written one this module emitted
        before source builds existed.
        """
        etc = f"/etc/{self.user}"
        return SourceBuild(
            name="nethermind",
            source=self.source,
            build=DotnetBuild(
                project=self.project_path,
                output="nethermind",
                runtime=self.runtime,
                restore_args=("--disable-parallel", "--force"),
                properties=NETHERMIND_PUBLISH_PROPERTIES,
                env=NETHERMIND_DOTNET_ENV,
                packages=(),
            ),
            install=(
                Install.artifact("/usr/bin/nethermind"),
                Install.file("publish/NLog.config", f"{etc}/NLog.config", mode="0644"),
                Install.tree("publish/plugins", f"{etc}/plugins"),
            ),
            cache_key=f"nethermind-{self.version}-{self.runtime}",
            mark_unpinned=False,
        )

    def _resolve_after(self, image: Image) -> tuple[str, ...]:
        return resolve_after(self.after, image)

    def _add_runtime_config(self, image: Image) -> None:
        """Add runtime config, unit file, and user creation."""
        resolved_after = self._resolve_after(image)
        image.file(
            "/usr/lib/systemd/system/nethermind-surge.service",
            content=self._render_service_unit(after=resolved_after),
        )

        for src_path, dest_path in self.config_files.items():
            image.file(dest_path, src=src_path)

        image.shell(
            f"mkosi-chroot useradd --system --home-dir /home/{self.user} "
            f"--shell /usr/sbin/nologin --groups {self.group} {self.user}",
            phase="postinst",
        )
        image.enable("nethermind-surge")

    def _render_service_unit(self, *, after: tuple[str, ...] | None = None) -> str:
        """Render nethermind-surge.service systemd unit."""
        effective = after if after is not None else self.after
        lines = ["[Unit]", "Description=Nethermind Surge"]
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
                "LimitNOFILE=1048576",
                "EnvironmentFile=/etc/nethermind-surge/env",
                "ExecStart=/usr/bin/nethermind \\",
                "--config /etc/nethermind-surge/config.json \\",
                "--datadir /home/nethermind-surge/data \\",
                "--JsonRpc.EngineHost 0.0.0.0 \\",
                "--JsonRpc.EnginePort 8551",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )
        return "\n".join(lines)
