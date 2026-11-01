"""The Taiko workload: the ``taiko-client`` driver."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from tundravm._image._component import Component, Endpoint
from tundravm._image._secrets import Secret
from tundravm._image._service import Service
from tundravm._image._values import TCP, Egress, egress_host
from tundravm.declarative import model as dm
from tundravm.errors import ValidationError

__all__ = ["TAIKO_CLIENT_SOURCE", "TaikoClient"]

TAIKO_CLIENT_SOURCE = dm.Git(
    "https://github.com/NethermindEth/surge-taiko-mono",
    "feat/tdx-proving",
    subdir="packages/taiko-client",
)
"""The taiko-client of NethermindEth's Taiko monorepo; ``tundravm lock`` pins it to a commit."""

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
