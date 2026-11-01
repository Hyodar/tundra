"""Nethermind, the .NET execution client, as a component: the one this example writes."""

from __future__ import annotations

from collections.abc import Iterator

from tundravm import Service
from tundravm.components import Component, Diagnostic, Egress, Endpoint, Resolved, Rule
from tundravm.ports import TCP, UDP
from tundravm.secrets import Generated, Secret
from tundravm.sources import Build, Git, Install
from tundravm.storage import VolumeDirectory
from tundravm.systemd import TcpProbe

SOURCE = Git("https://github.com/NethermindEth/nethermind.git", "1.32.3")
HOME = "/usr/lib/nethermind"
RUNNER = "src/Nethermind/Nethermind.Runner --runtime linux-x64"
PROPERTIES = {
    "Deterministic": "true",
    "ContinuousIntegrationBuild": "true",
    "PublishSingleFile": "true",
    "BuildTimestamp": "0",
    "Commit": "0" * 40,
    "PublishReadyToRun": "false",
    "DebugType": "none",
    "IncludeAllContentForSelfExtract": "true",
    "EmbedUntrackedSources": "true",
}
PUBLISH = (
    f"dotnet restore {RUNNER} --disable-parallel --force && "
    f"dotnet publish {RUNNER} --configuration Release --self-contained true --output publish "
    + " ".join(f"-p:{key}={value}" for key, value in PROPERTIES.items())
)
TOOLCHAIN = ("dotnet-sdk-10.0", "build-essential", "git")
INSTALL = (
    Install("publish/nethermind", f"{HOME}/nethermind"),
    Install("publish/NLog.config", f"{HOME}/NLog.config", mode=0o644),
    Install("publish/plugins", f"{HOME}/plugins", mode=None, directory=True),
)
DOTNET = {
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "DOTNET_NOLOGO": "1",
    "DOTNET_CLI_HOME": "/tmp/dotnet",
    "NUGET_PACKAGES": "/tmp/nuget",
}


class Nethermind(Component, build_network=True):
    """Nethermind on *config*, its JSON-RPC and Engine API on the loopback."""

    jwt: Secret | Generated
    state: VolumeDirectory | None = None
    config: str = "hoodi"
    rpc_port: int = 8545
    engine_port: int = 8551

    @property
    def rpc(self) -> Endpoint:
        return self.service("nethermind").endpoint(self.rpc_port)

    @property
    def engine_api(self) -> Endpoint:
        return self.service("nethermind").endpoint(self.engine_port)

    def outbound(self) -> Egress:
        return Egress(ports=[TCP(30303), UDP(30303)])

    def compose(self) -> Iterator[object]:
        yield Build(
            "nethermind",
            SOURCE,
            script=PUBLISH,
            packages=TOOLCHAIN,
            env=DOTNET,
            install=INSTALL,
            cache_namespace=f"nethermind-{SOURCE.ref}",
        )
        datadir = "/var/lib/nethermind" if self.state is None else self.state.path
        yield Service(
            "nethermind",
            [
                f"{HOME}/nethermind",
                f"--config={self.config}",
                f"--datadir={datadir}",
                "--JsonRpc.Enabled=true",
                "--JsonRpc.Host=127.0.0.1",
                f"--JsonRpc.Port={self.rpc_port}",
                "--JsonRpc.EngineHost=127.0.0.1",
                f"--JsonRpc.EnginePort={self.engine_port}",
                f"--JsonRpc.JwtSecretFile={self.jwt.path}",
            ],
            readiness=TcpProbe(self.rpc_port, timeout="10min"),
            state="nethermind" if self.state is None else self.state,
            credentials=[self.jwt],
            hardening="dotnet",
        )

    rules = (
        Rule(
            "nethermind-volatile-state",
            "warning",
            "Nethermind keeps its database on the volatile root, so it resyncs at every boot.",
            "Pass state=volume.directory('nethermind') on a Volume.",
        ),
    )

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        if self.state is not None:
            return ()
        message = "Nethermind keeps its database on the volatile root: it resyncs every boot"
        return (Diagnostic("nethermind-volatile-state", "warning", message),)
