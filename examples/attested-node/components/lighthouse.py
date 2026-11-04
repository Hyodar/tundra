"""Lighthouse, the Rust consensus client, from its prebuilt release, as a beacon node.

The same shape as ``nethermind.py``: a download in place of a build, an HTTP readiness
probe, two declared rules and the checkpoint host in its ``outbound()``.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit

from tundravm import Package, Service
from tundravm.components import Component, Diagnostic, Endpoint, Resolved, Rule
from tundravm.components.network import Egress
from tundravm.errors import ValidationError
from tundravm.ports import TCP, UDP
from tundravm.secrets import Generated, Secret
from tundravm.sources import Build, Http, Install
from tundravm.storage import VolumeDirectory
from tundravm.systemd import DropIn, HttpProbe

RELEASE = "v8.3.2"
SOURCE = Http(
    "https://github.com/sigp/lighthouse/releases/download/"
    f"{RELEASE}/lighthouse-{RELEASE}-x86_64-unknown-linux-gnu.tar.gz",
    sha256="ea914e932462d6a93ad78c21377d70f995e05ac224248be0b58c2f4d6740439a",
)
NETWORKS = ("mainnet", "gnosis", "chiado", "sepolia", "holesky", "hoodi")
HOME = "/usr/lib/lighthouse"
P2P: list[TCP | UDP] = [TCP(9000), UDP(9000), UDP(9001)]
HEALTH = "/eth/v1/node/health"


class Lighthouse(Component):
    """A beacon node of *network* driving *execution* (an Engine API) with *jwt*.

    It installs Sigma Prime's x86-64 release tarball, pinned by sha256, with the C++
    runtime and zlib it links. It syncs from *checkpoint_sync_url*, a beacon node you
    trust: Lighthouse refuses to sync from genesis, so without one it exits at start.
    Its HTTP API is ready once ``/eth/v1/node/health`` answers 200 or 206 (syncing). It
    dials peers on 9000 and 9001 and accepts none unless *expose_p2p*. *disable_quic*
    drops the QUIC transport (and UDP 9001 with it), *disable_optimistic_finalized_sync*
    and *state_cache_size* pass Lighthouse's flags of those names.
    """

    execution: Endpoint
    jwt: Secret | Generated
    checkpoint_sync_url: str | None = None
    network: str = "hoodi"
    state: VolumeDirectory | None = None
    http_port: int = 5052
    expose_p2p: bool = False
    disable_quic: bool = False
    disable_optimistic_finalized_sync: bool = False
    state_cache_size: int | None = None

    def __post_init__(self) -> None:
        if self.network not in NETWORKS:
            raise ValidationError(
                f"Lighthouse: network {self.network!r} is not one Lighthouse ships.",
                hint=f"Pass one of: {', '.join(NETWORKS)}.",
            )
        url = self.checkpoint_sync_url
        if url is not None and not (url.startswith(("https://", "http://")) and _host(url)):
            raise ValidationError(
                f"Lighthouse: checkpoint_sync_url {url!r} is not an http(s) URL with a host.",
                hint="Pass the beacon node's URL: checkpoint_sync_url='https://checkpoint.example'.",
            )

    @property
    def http(self) -> Endpoint:
        """The beacon node API, on the loopback."""
        return self.service("lighthouse").endpoint(self.http_port)

    def outbound(self) -> Egress:
        """The p2p ports and the checkpoint URL's ``HOST:PORT``."""
        url = self.checkpoint_sync_url
        return Egress(ports=self._p2p(), hosts=[] if url is None else [_host(url)])

    def _p2p(self) -> list[TCP | UDP]:
        return P2P[:2] if self.disable_quic else P2P

    def compose(self) -> Iterator[object]:
        yield Build(
            "lighthouse",
            SOURCE,
            script=f"tar -xzf {SOURCE.url.rpartition('/')[2]} lighthouse",
            install=(Install("lighthouse", f"{HOME}/lighthouse"),),
            cache_namespace=f"lighthouse-{RELEASE}",
        )
        yield Package("libstdc++6")
        yield Package("zlib1g")
        datadir = "/var/lib/lighthouse" if self.state is None else self.state.path
        argv = [
            f"{HOME}/lighthouse",
            "bn",
            f"--network={self.network}",
            f"--datadir={datadir}",
            f"--execution-endpoint={self.execution.url}",
            f"--execution-jwt={self.jwt.path}",
            "--http",
            "--http-address=127.0.0.1",
            f"--http-port={self.http_port}",
            "--port=9000",
            "--disable-upnp",
        ]
        if self.disable_quic:
            argv.append("--disable-quic")
        if self.disable_optimistic_finalized_sync:
            argv.append("--disable-optimistic-finalized-sync")
        if self.state_cache_size is not None:
            argv.append(f"--state-cache-size={self.state_cache_size}")
        if self.checkpoint_sync_url is not None:
            argv.append(f"--checkpoint-sync-url={self.checkpoint_sync_url}")
        yield Service(
            "lighthouse",
            argv,
            requires=[self.execution],
            readiness=HttpProbe(f"http://127.0.0.1:{self.http_port}{HEALTH}", timeout="10min"),
            state="lighthouse" if self.state is None else self.state,
            credentials=[self.jwt],
            expose=self._p2p() if self.expose_p2p else [],
            hardening="static",
            unit=DropIn(limits={"NOFILE": 65536}),
        )

    rules = (
        Rule(
            "lighthouse-checkpoint-missing",
            "error",
            "Lighthouse has no checkpoint_sync_url, and it refuses to sync from genesis, so "
            "the beacon node exits at start.",
            "Pass checkpoint_sync_url= a beacon node you trust serves the finalized state at.",
        ),
        Rule(
            "lighthouse-volatile-state",
            "warning",
            "Lighthouse keeps its database on the volatile root, so it syncs again from the "
            "checkpoint at every boot.",
            "Pass state=volume.directory('lighthouse') on a Volume.",
        ),
    )

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        """``lighthouse-checkpoint-missing`` and ``lighthouse-volatile-state``."""
        found = []
        if self.checkpoint_sync_url is None:
            message = "Lighthouse has no checkpoint_sync_url: it exits at start"
            found.append(Diagnostic("lighthouse-checkpoint-missing", "error", message))
        if self.state is None:
            message = "Lighthouse keeps its database on the volatile root: it syncs every boot"
            found.append(Diagnostic("lighthouse-volatile-state", "warning", message))
        return tuple(found)


def _host(url: str) -> str:
    """*url*'s ``HOST:PORT``, the port its scheme implies when it names none."""
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.hostname}:{port}" if parts.hostname else ""
