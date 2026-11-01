"""Nethermind, the .NET execution client, as a component: the one this example writes."""

from __future__ import annotations

from collections.abc import Iterator

from tundravm import Package, Service
from tundravm.components import Component, Diagnostic, Egress, Endpoint, Resolved, Rule
from tundravm.ports import TCP, UDP
from tundravm.secrets import Generated, Secret
from tundravm.sources import Build, Dotnet, Git, Install
from tundravm.storage import VolumeDirectory
from tundravm.systemd import TcpProbe

SOURCE = Git("https://github.com/NethermindEth/nethermind.git", "1.32.3")
HOME = "/usr/lib/nethermind"
PROPERTIES = {
    "PublishSingleFile": "true",
    "BuildTimestamp": "0",
    "Commit": "0" * 40,
    "PublishReadyToRun": "false",
    "DebugType": "none",
    "IncludeAllContentForSelfExtract": "true",
    "EmbedUntrackedSources": "true",
}
INSTALL = (
    Install("publish/nethermind", f"{HOME}/nethermind"),
    Install("publish/NLog.config", f"{HOME}/NLog.config", mode=0o644),
    Install("publish/plugins", f"{HOME}/plugins", mode=None, directory=True),
)
RUNTIME = ("libssl3t64", "libstdc++6")
"""What the self-contained publish needs from Debian at runtime: OpenSSL for its TLS and
hashes, the C++ runtime for the CLR. It is built with invariant globalization, so no ICU."""


class Nethermind(Component):
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
            using=Dotnet(
                project="src/Nethermind/Nethermind.Runner",
                output="nethermind",
                properties=PROPERTIES,
            ),
            install=INSTALL,
            cache_namespace=f"nethermind-{SOURCE.ref}",
        )
        for package in RUNTIME:
            yield Package(package)
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
