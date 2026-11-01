"""Lighthouse, the Rust consensus client, from its prebuilt release, as a beacon node."""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit

from tundravm._image._component import Component, Endpoint
from tundravm._image._secrets import Generated, Secret
from tundravm._image._service import Service
from tundravm._image._storage import VolumeDirectory
from tundravm._image._values import TCP, UDP, Egress, HttpProbe, Resources
from tundravm.declarative import model as dm
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError

from ._observability import _destination

__all__ = ["LIGHTHOUSE_RELEASE", "LIGHTHOUSE_SOURCE", "NETWORKS", "Lighthouse"]

LIGHTHOUSE_RELEASE = "v8.3.2"
"""The Lighthouse release the component installs."""

LIGHTHOUSE_SOURCE = dm.Http(
    "https://github.com/sigp/lighthouse/releases/download/"
    f"{LIGHTHOUSE_RELEASE}/lighthouse-{LIGHTHOUSE_RELEASE}-x86_64-unknown-linux-gnu.tar.gz",
    sha256="ea914e932462d6a93ad78c21377d70f995e05ac224248be0b58c2f4d6740439a",
)
"""The portable x86-64 build of :data:`LIGHTHOUSE_RELEASE`, Sigma Prime's own tarball."""

NETWORKS = ("mainnet", "gnosis", "chiado", "sepolia", "holesky", "hoodi")
"""The networks Lighthouse ships (``lighthouse bn --network``)."""

_HOME = "/usr/lib/lighthouse"
_P2P_PORT = 9000
_QUIC_PORT = _P2P_PORT + 1
_HEALTH = "/eth/v1/node/health"
_RUNTIME_PACKAGES = ("libstdc++6", "zlib1g")
_OPEN_FILES = 65536


class Lighthouse(Component):
    """Lighthouse, a beacon node of *network* driving *execution* through the Engine API.

    The workload contract: the ``lighthouse`` binary of Lighthouse v8.3.2, the portable
    ``x86_64-unknown-linux-gnu`` tarball Sigma Prime publishes (pinned by its sha256, the
    digest GitHub records for the release asset, whose PGP signature is Sigma Prime's),
    installed as ``/usr/lib/lighthouse/lighthouse`` with no build, beside the C++ runtime
    and zlib it links. It runs ``lighthouse bn`` on *network* (one Lighthouse ships,
    :data:`NETWORKS`), calls *execution* (the execution client's Engine API,
    ``nethermind.engine_api``) with *jwt*, the same secret the execution client reads, and
    keeps its database in *state* (a volume directory; ``None`` keeps it on the volatile
    root, which :meth:`check` reports). It starts once *execution* is ready, under the
    ``static`` hardening preset, which denies ``setrlimit``, so the unit sets the
    open-file limit (65536) Lighthouse would otherwise raise itself.

    *checkpoint_sync_url* is a beacon node to fetch a recent finalized state from.
    Lighthouse refuses to sync from genesis (a node that does is open to long-range
    attacks once the chain is older than its weak subjectivity period, which every
    network it ships is), so without one the node exits at start; :meth:`check` reports
    that as an error. Use one you trust: it decides which chain the node follows.

    Its HTTP API listens on ``127.0.0.1`` at *http_port* and is ready once
    ``/eth/v1/node/health`` answers 200 (synced) or 206 (syncing): a node still syncing
    is serving. :meth:`outbound` is the p2p port, 9000 over TCP and UDP (discovery) and
    9001 over UDP (QUIC), and the checkpoint URL's ``HOST:PORT``. Inbound p2p is closed:
    a node in a TD need not accept peers, it dials them. *expose_p2p* opens 9000/9001 for
    peers that dial in.

    Exports :attr:`http`, the beacon API.
    """

    execution: Endpoint
    jwt: Secret | Generated
    checkpoint_sync_url: str | None = None
    network: str = "hoodi"
    state: VolumeDirectory | None = None
    http_port: int = 5052
    expose_p2p: bool = False
    resources: Resources | None = None
    source: dm.Http = LIGHTHOUSE_SOURCE

    def __post_init__(self) -> None:
        if not isinstance(self.execution, Endpoint) or self.execution.ready is None:
            raise ValidationError(
                f"Lighthouse: execution= must be an Endpoint with readiness, got "
                f"{self.execution!r}.",
                hint="Pass the execution client's Engine API: execution=nethermind.engine_api.",
            )
        if not isinstance(self.jwt, (Secret, Generated)):
            raise ValidationError(
                "Lighthouse: jwt= must be the secret the execution client reads.",
                hint="Pass the same Generated('jwt') (or Secret('jwt')) to both clients.",
            )
        if self.network not in NETWORKS:
            raise ValidationError(
                f"Lighthouse: network {self.network!r} is not one Lighthouse ships.",
                hint=f"Pass one of: {', '.join(NETWORKS)}.",
            )
        if self.checkpoint_sync_url is not None:
            _check_url(self.checkpoint_sync_url)
        if not isinstance(self.expose_p2p, bool):
            raise ValidationError(
                "Lighthouse: expose_p2p= must be True or False.",
                hint="Leave it off unless peers must dial this node.",
            )

    @property
    def http(self) -> Endpoint:
        """The beacon node API, on the loopback."""
        return self.service("lighthouse").endpoint(self.http_port)

    def outbound(self) -> Egress:
        """The p2p ports, the checkpoint URL's ``HOST:PORT`` and *execution* when it is not
        on the loopback."""
        hosts = []
        if self.checkpoint_sync_url is not None:
            hosts.append(_destination(self.checkpoint_sync_url))
        if not self.execution.loopback:
            hosts.append(self.execution.address)
        return Egress(ports=_p2p(), hosts=hosts)

    def compose(self) -> Iterator[object]:
        tarball = self.source.url.rpartition("/")[2]
        yield dm.Build(
            "lighthouse",
            self.source,
            script=f"tar -xzf {tarball} lighthouse",
            install=(dm.Install("lighthouse", f"{_HOME}/lighthouse"),),
            cache_namespace=f"lighthouse-{LIGHTHOUSE_RELEASE}",
        )
        yield from (dm.Package(name) for name in _RUNTIME_PACKAGES)
        datadir = "/var/lib/lighthouse" if self.state is None else self.state.path
        argv = [
            f"{_HOME}/lighthouse",
            "bn",
            f"--network={self.network}",
            f"--datadir={datadir}",
            f"--execution-endpoint={self.execution.url}",
            f"--execution-jwt={self.jwt.path}",
            "--http",
            "--http-address=127.0.0.1",
            f"--http-port={self.http_port}",
            f"--port={_P2P_PORT}",
            "--disable-upnp",
        ]
        if self.checkpoint_sync_url is not None:
            argv.append(f"--checkpoint-sync-url={self.checkpoint_sync_url}")
        yield Service(
            "lighthouse",
            argv,
            requires=[self.execution],
            readiness=HttpProbe(f"http://127.0.0.1:{self.http_port}{_HEALTH}", timeout="10min"),
            state="lighthouse" if self.state is None else self.state,
            credentials=[self.jwt],
            expose=_p2p() if self.expose_p2p else (),
            resources=self.resources,
            hardening="static",
            unit=dm.DropIn(limits={"NOFILE": _OPEN_FILES}),
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

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``lighthouse-checkpoint-missing``, ``lighthouse-volatile-state``."""
        found: list[dm.Diagnostic] = []
        if self.checkpoint_sync_url is None:
            found.append(
                dm.Diagnostic(
                    "lighthouse-checkpoint-missing",
                    "error",
                    "Lighthouse has no checkpoint_sync_url: it refuses to sync from genesis "
                    "and exits at start",
                )
            )
        if self.state is None:
            found.append(
                dm.Diagnostic(
                    "lighthouse-volatile-state",
                    "warning",
                    "Lighthouse keeps its database on the volatile root: it syncs again at "
                    "every boot (pass state=volume.directory('lighthouse'))",
                )
            )
        return tuple(found)


def _p2p() -> list[TCP | UDP]:
    return [TCP(_P2P_PORT), UDP(_P2P_PORT), UDP(_QUIC_PORT)]


def _check_url(url: object) -> None:
    hint = "Pass the beacon node's URL: checkpoint_sync_url='https://checkpoint.example'."
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ValidationError(
            f"Lighthouse: checkpoint_sync_url {url!r} is not an http(s) URL.", hint=hint
        )
    try:
        parts = urlsplit(url)
        named = bool(parts.hostname) and parts.port != 0
    except ValueError:
        named = False
    if not named or any(c.isspace() or c in "'\"" for c in url):
        raise ValidationError(
            f"Lighthouse: checkpoint_sync_url {url!r} names no host, or a bad port.", hint=hint
        )
