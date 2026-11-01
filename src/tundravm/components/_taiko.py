"""The Taiko/Surge workloads: the ``taiko-client`` driver and the Raiko TDX prover."""

from __future__ import annotations

from collections.abc import Iterator

from tundravm._image._component import Component, Endpoint
from tundravm._image._secrets import Secret
from tundravm._image._service import Service
from tundravm._image._storage import VolumeDirectory
from tundravm._image._values import TcpProbe
from tundravm.declarative import model as dm
from tundravm.declarative.systemd import Hardening
from tundravm.errors import ValidationError

from ._base import EVIDENCE

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
    """

    engine: Endpoint
    rpc: Endpoint
    jwt: Secret
    chain: Secret
    source: dm.Git = TAIKO_CLIENT_SOURCE

    def __post_init__(self) -> None:
        _endpoint("TaikoClient", "engine", self.engine)
        _endpoint("TaikoClient", "rpc", self.rpc)
        for name in ("jwt", "chain"):
            if not isinstance(getattr(self, name), Secret):
                raise ValidationError(
                    f"TaikoClient: {name}= must be a Secret the image declares.",
                    hint="Declare the Secret once: pass it to the image's secrets= and here.",
                )

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
    *state* keeps its proof cache.

    Exports :attr:`http`, ready once the listener accepts connections: put it behind
    ``AttestedTLS(443, upstream=raiko.http)``, the only way requests should reach it.
    """

    config: Secret
    state: VolumeDirectory | None = None
    listen: str = "127.0.0.1:8080"
    l2_rpc: Endpoint | None = None
    source: dm.Git = RAIKO_SOURCE

    def __post_init__(self) -> None:
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
            requires=[EVIDENCE] if self.l2_rpc is None else [EVIDENCE, self.l2_rpc],
            readiness=TcpProbe(int(port), host=host),
            state="raiko" if self.state is None else self.state,
            credentials=[self.config],
            groups=["tdx"],
            hardening=Hardening.preset("static", devices=["/dev/tdx_guest"]),
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
