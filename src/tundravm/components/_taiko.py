"""The Taiko/Surge workloads: the ``taiko-client`` driver and the Raiko TDX prover."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from tundravm._image._component import Component, Endpoint
from tundravm._image._secrets import Secret
from tundravm._image._service import Service
from tundravm._image._storage import VolumeDirectory
from tundravm._image._values import TCP, Egress, TcpProbe, egress_host
from tundravm.declarative import model as dm
from tundravm.declarative.systemd import Hardening
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError

from ._base import INTEL_PCS

__all__ = ["RAIKO_SOURCE", "TAIKO_CLIENT_SOURCE", "Raiko", "TaikoClient"]

RAIKO_SOURCE = dm.Git("https://github.com/NethermindEth/raiko.git", "feat/tdx")
"""The Raiko branch with the TDX prover; ``tundravm lock`` pins it to a commit."""

TAIKO_CLIENT_SOURCE = dm.Git(
    "https://github.com/NethermindEth/surge-taiko-mono",
    "feat/tdx-proving",
    subdir="packages/taiko-client",
)
"""The taiko-client of the Surge monorepo; ``tundravm lock`` pins it to a commit."""

_RAIKO_BUILD = dm.Cargo(
    output="raiko-host",
    package="raiko-host",
    features=("tdx",),
    env={"CARGO_PROFILE_RELEASE_LTO": "thin", "CARGO_PROFILE_RELEASE_PANIC": "abort"},
    packages=("build-essential", "pkg-config", "git", "clang", "cargo", "libssl-dev"),
)
"""``cargo auditable build --release --frozen --features tdx --package raiko-host`` with the
toolchain raiko's ``rust-toolchain`` pins, and the reproducible flags ``Cargo`` adds (generic
CPU, no build id, v0 symbols, one codegen unit, no incremental build)."""
_TAIKO_TOOLCHAIN = ("golang", "git", "build-essential")
_TAIKO_BUILD = (
    'GO111MODULE=on CGO_CFLAGS="-O -D__BLST_PORTABLE__" '
    'CGO_CFLAGS_ALLOW="-O -D__BLST_PORTABLE__" '
    'go build -trimpath -buildvcs=false -ldflags "-s -w -buildid=" -o bin/taiko-client cmd/main.go'
)


def _remote(*endpoints: Endpoint | None) -> list[str]:
    """``HOST:PORT`` of each of *endpoints* beyond the loopback."""
    return list(dict.fromkeys(e.address for e in endpoints if e is not None and not e.loopback))


def _l1(owner: object, value: object) -> tuple[str, ...]:
    """*value*, the ``l1=`` of *owner*, as the ``HOST:PORT`` of each L1 endpoint."""
    kind = type(owner).__name__
    hint = "Pass l1=['l1.example:8546', 'beacon.example:5052'], each endpoint's HOST:PORT."
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValidationError(f"{kind}: l1= takes a list of HOST:PORT.", hint=hint)
    for entry in value:
        try:
            port = egress_host(owner, entry)[1]
        except ValidationError:
            port = None
        if port is None:
            raise ValidationError(f"{kind}: l1= entry {entry!r} is not HOST:PORT.", hint=hint)
    return tuple(value)


def _endpoint(owner: str, name: str, value: object) -> Endpoint:
    if not isinstance(value, Endpoint) or value.ready is None:
        raise ValidationError(
            f"{owner}: {name}= must be an Endpoint with readiness, got {value!r}.",
            hint="Pass the export of the component that serves it: execution.rpc.",
        )
    return value


class TaikoClient(Component, build_network=True):
    """The ``taiko-client`` driver: follows L1 and drives the execution client's Engine
    API, so the L2 chain advances.

    The workload contract: a ``go build`` of ``cmd/main.go`` with the portable BLST flags
    (the build fetches Go modules, so the image builds with the network on), run as
    ``taiko-client driver`` against *engine* (the Engine API, authenticated with *jwt*)
    and *rpc* (the execution client's JSON-RPC, over HTTP and WebSocket). *chain* is the
    deployment's L1 side, delivered as an environment file because L1 endpoints carry
    provider keys: ``L1_WS``, ``L1_BEACON``, ``TAIKO_ANCHOR``, ``TAIKO_TOKEN`` and the
    inbox address the protocol fork needs (``PACAYA_INBOX``...), the variables
    taiko-client's flags read. It starts once both endpoints are ready.

    *l1* is the ``HOST:PORT`` of each endpoint *chain* names (``L1_WS``, ``L1_BEACON``),
    which :meth:`outbound` accepts and nothing else of L1's. *chain* is delivered after the
    image is built, so without *l1* the build cannot read them, and :meth:`outbound` is TCP
    443 to any address, the HTTPS and WSS port of L1 providers.
    """

    engine: Endpoint
    rpc: Endpoint
    jwt: Secret
    chain: Secret
    l1: Sequence[str] = ()
    source: dm.Git = TAIKO_CLIENT_SOURCE

    def __post_init__(self) -> None:
        object.__setattr__(self, "l1", _l1(self, self.l1))
        _endpoint("TaikoClient", "engine", self.engine)
        _endpoint("TaikoClient", "rpc", self.rpc)
        for name in ("jwt", "chain"):
            if not isinstance(getattr(self, name), Secret):
                raise ValidationError(
                    f"TaikoClient: {name}= must be a Secret.",
                    hint=f"Pass {name}=Secret('{name}'), delivered after attestation.",
                )

    def outbound(self) -> Egress:
        """*l1* (TCP 443 without it), and *engine* and *rpc* when they are not on the
        loopback."""
        hosts = list(dict.fromkeys([*self.l1, *_remote(self.engine, self.rpc)]))
        return Egress(ports=[] if self.l1 else [TCP(443)], hosts=hosts)

    def compose(self) -> Iterator[object]:
        yield dm.Build(
            "taiko-client",
            self.source,
            script=_TAIKO_BUILD,
            packages=_TAIKO_TOOLCHAIN,
            install=(dm.Install("bin/taiko-client", "/usr/bin/taiko-client"),),
            cache_namespace=f"taiko-client-{self.source.ref}",
        )
        driver = (
            f'set -a; . "{self.chain.path}"; set +a; '
            "exec /usr/bin/taiko-client driver "
            f"--l2.ws={self.rpc.with_scheme('ws').url} --l2.http={self.rpc.url} "
            f'--l2.auth={self.engine.url} --jwtSecret="{self.jwt.path}"'
        )
        yield Service(
            "taiko-client",
            ["/bin/sh", "-eu", "-c", driver],
            requires=[self.engine, self.rpc],
            credentials=[self.jwt, self.chain],
        )


class Raiko(Component, build_network=True):
    """Raiko, the TDX prover: proves L2 blocks inside the TD and answers on *listen*.

    The workload contract: ``raiko-host`` from *source* built with ``cargo`` and the
    ``tdx`` feature, with the Rust toolchain the source pins (``tundravm build`` installs it
    and fetches the crates on the host; the image builds with the network on for raiko's
    build scripts),
    installed as ``/usr/bin/raiko``, run in the ``tdx`` group after the evidence service
    is ready, since every proof carries a quote. *config* is Raiko's configuration file,
    delivered because it names the L1 and L2 endpoints and their provider keys; *l2_rpc*,
    when given, is the execution client it proves blocks of, and Raiko starts after it.
    *state* keeps its proof cache. *l1* is the ``HOST:PORT`` of each L1 endpoint *config*
    names. :meth:`outbound` is Intel's collateral service over HTTPS and *l1*; the
    collateral rule is TCP 443 to any address (nftables matches no names), which also
    carries the HTTPS endpoints *config* names, so without *l1* an endpoint on 443 needs
    nothing more.

    Exports :attr:`http`, ready once the listener accepts connections: put it behind
    ``AttestedTLS(443, upstream=raiko.http)``, the only way requests should reach it.
    """

    config: Secret
    state: VolumeDirectory | None = None
    listen: str = "127.0.0.1:8080"
    l2_rpc: Endpoint | None = None
    l1: Sequence[str] = ()
    source: dm.Git = RAIKO_SOURCE

    def __post_init__(self) -> None:
        object.__setattr__(self, "l1", _l1(self, self.l1))
        if not isinstance(self.config, Secret):
            raise ValidationError(
                "Raiko: config= must be the Secret the image declares.",
                hint="Declare config = Secret('raiko-config') and pass it to the image and Raiko.",
            )
        host, _, port = self.listen.rpartition(":")
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValidationError(
                f"Raiko: listen {self.listen!r} is not HOST:PORT.",
                hint="Pass listen='127.0.0.1:8080'.",
            )
        if self.l2_rpc is not None:
            _endpoint("Raiko", "l2_rpc", self.l2_rpc)

    @property
    def http(self) -> Endpoint:
        """Raiko's HTTP API."""
        host, _, port = self.listen.rpartition(":")
        return Endpoint(host, int(port), ready=self.service("raiko").ready)

    def outbound(self) -> Egress:
        """Intel's PCS over HTTPS (TDX collateral), *l1*, and *l2_rpc* when it is not on the
        loopback."""
        return Egress(hosts=list(dict.fromkeys([INTEL_PCS, *self.l1, *_remote(self.l2_rpc)])))

    def compose(self) -> Iterator[object]:
        yield dm.Build(
            "raiko",
            self.source,
            using=_RAIKO_BUILD,
            install=(dm.Install(_RAIKO_BUILD.artifact, "/usr/bin/raiko"),),
            cache_namespace=f"raiko-{self.source.ref}",
        )
        host, _, port = self.listen.rpartition(":")
        yield Service(
            "raiko",
            ["/usr/bin/raiko", f"--address={self.listen}", f"--config-path={self.config.path}"],
            requires=[] if self.l2_rpc is None else [self.l2_rpc],
            readiness=TcpProbe(int(port), host=host),
            state="raiko" if self.state is None else self.state,
            credentials=[self.config],
            quotes=True,
            hardening=Hardening.preset("static", devices=["/dev/tdx_guest"]),
        )

    rules = (
        Rule(
            "raiko-listens-publicly",
            "warning",
            "Raiko listens beyond the loopback, so requests reach the prover without going "
            "through its AttestedTLS ingress.",
            "Keep Raiko(listen='127.0.0.1:PORT'), the default, and reach it through the ingress.",
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``raiko-listens-publicly``: a prover reachable around its attested ingress."""
        if self.http.loopback:
            return ()
        return (
            dm.Diagnostic(
                "raiko-listens-publicly",
                "warning",
                f"Raiko listens on {self.listen}: requests can reach it without going "
                "through AttestedTLS (listen on 127.0.0.1)",
            ),
        )
