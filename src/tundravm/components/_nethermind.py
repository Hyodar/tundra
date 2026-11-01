"""Nethermind, the .NET execution client, built from source with a deterministic publish."""

from __future__ import annotations

from collections.abc import Iterator

from tundravm._image._component import Component, Endpoint
from tundravm._image._secrets import Secret
from tundravm._image._service import Service
from tundravm._image._storage import VolumeDirectory
from tundravm._image._values import Resources, TcpProbe
from tundravm.declarative import model as dm
from tundravm.declarative.systemd import Hardening
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError

__all__ = ["NETHERMIND_SOURCE", "Nethermind"]

NETHERMIND_SOURCE = dm.Git("https://github.com/NethermindEth/nethermind.git", "1.32.3")
"""The Nethermind release the component builds; ``tundravm lock`` pins it to a commit."""

_PROJECT = "src/Nethermind/Nethermind.Runner"
_RUNTIME = "linux-x64"
_HOME = "/usr/lib/nethermind"
_TOOLCHAIN = ("dotnet-sdk-10.0", "build-essential", "git")
_DOTNET_ENV = {
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "DOTNET_NOLOGO": "1",
    "DOTNET_CLI_HOME": "/tmp/dotnet",
    "NUGET_PACKAGES": "/tmp/nuget",
}
_PUBLISH = {
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


def _publish_script() -> str:
    properties = " ".join(f"-p:{k}={v}" for k, v in _PUBLISH.items())
    return (
        f"dotnet restore {_PROJECT} --runtime {_RUNTIME} --disable-parallel --force && "
        f"dotnet publish {_PROJECT} --configuration Release --runtime {_RUNTIME} "
        f"--self-contained true --output publish {properties}"
    )


class Nethermind(Component, build_network=True):
    """Nethermind at *source*, serving JSON-RPC and the Engine API on the loopback.

    The workload contract: a ``dotnet publish`` with the .NET 10 SDK, deterministic
    properties and the restore it needs (the build reaches NuGet, so the image builds
    with the network on), installed under ``/usr/lib/nethermind`` with its plugins; the
    network configuration *config* is one Nethermind ships (``surge-hoodi``,
    ``taiko-alethia``...). It reads the Engine API secret *jwt* as a credential, keeps its
    database in *state* (a volume directory; ``None`` keeps it on the volatile root, so
    it resyncs at every boot, which :meth:`check` reports), and runs under the ``dotnet``
    hardening preset.

    Exports :attr:`rpc` (JSON-RPC over HTTP), :attr:`ws` (the same port over WebSocket)
    and :attr:`engine_api`, each ready once the RPC port accepts connections.
    """

    jwt: Secret
    state: VolumeDirectory | None = None
    config: str = "surge-hoodi"
    source: dm.Git = NETHERMIND_SOURCE
    rpc_port: int = 8545
    engine_port: int = 8551
    resources: Resources | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.jwt, Secret):
            raise ValidationError(
                "Nethermind: jwt= must be the Secret the image declares.",
                hint="Declare jwt = Secret('jwt') and pass it to both the image and Nethermind.",
            )
        if not isinstance(self.config, str) or not self.config or "/" in self.config:
            raise ValidationError(
                f"Nethermind: config {self.config!r} is not a configuration name.",
                hint="Name one Nethermind ships, e.g. config='surge-hoodi'.",
            )
        if self.rpc_port == self.engine_port:
            raise ValidationError(
                "Nethermind: rpc_port and engine_port must differ.",
                hint="Leave the defaults (8545, 8551) or pick two ports.",
            )

    @property
    def rpc(self) -> Endpoint:
        """JSON-RPC over HTTP."""
        return Endpoint("127.0.0.1", self.rpc_port, ready=self.service("nethermind").ready)

    @property
    def ws(self) -> Endpoint:
        """JSON-RPC over WebSocket, on the RPC port."""
        return self.rpc.with_scheme("ws")

    @property
    def engine_api(self) -> Endpoint:
        """The Engine API a consensus driver calls with the shared JWT."""
        return Endpoint("127.0.0.1", self.engine_port, ready=self.service("nethermind").ready)

    def compose(self) -> Iterator[object]:
        yield dm.Build(
            "nethermind",
            self.source,
            script=_publish_script(),
            packages=_TOOLCHAIN,
            env=_DOTNET_ENV,
            install=(
                dm.Install("publish/nethermind", f"{_HOME}/nethermind"),
                dm.Install("publish/NLog.config", f"{_HOME}/NLog.config", mode=0o644),
                dm.Install("publish/plugins", f"{_HOME}/plugins", mode=None, directory=True),
            ),
            cache_namespace=f"nethermind-{self.source.ref}-{_RUNTIME}",
        )
        datadir = "/var/lib/nethermind" if self.state is None else self.state.path
        yield Service(
            "nethermind",
            [
                f"{_HOME}/nethermind",
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
            resources=self.resources,
            hardening=Hardening.preset("dotnet"),
        )

    rules = (
        Rule(
            "nethermind-volatile-state",
            "warning",
            "Nethermind keeps its database on the volatile root, so it resyncs from genesis at "
            "every boot.",
            "Pass state=volume.directory('nethermind') on a Volume.",
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``nethermind-volatile-state``: a database on the volatile root resyncs every boot."""
        if self.state is not None:
            return ()
        return (
            dm.Diagnostic(
                "nethermind-volatile-state",
                "warning",
                "Nethermind keeps its database on the volatile root: it resyncs from "
                "genesis at every boot (pass state=volume.directory('nethermind'))",
            ),
        )
