"""Ingress: how requests from outside reach a service that listens only inside the image.

``Service(expose=[TCP(8080)])`` opens a port and protects nothing. An ingress is the
other way in: a TLS listener on a public port that relays to an upstream
:class:`~tundravm.components.Endpoint` on the loopback interface, so the listener is the only
path to it. :class:`AttestedTLS` is the confidential one, :class:`TLS` ordinary HTTPS.
"""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass

from tundravm._location import Located
from tundravm.errors import ValidationError

from ._component import Endpoint
from ._secrets import Secret

__all__ = ["AttestedTLS", "Ingress", "TLS"]


def _check(owner: object, port: object, upstream: object) -> None:
    kind = type(owner).__name__
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ValidationError(
            f"{kind}: {port!r} is not a port number (1-65535).",
            hint="Pass the public port: AttestedTLS(443, upstream=...).",
        )
    if not isinstance(upstream, Endpoint):
        raise ValidationError(
            f"{kind}({port}): upstream {upstream!r} is not an Endpoint.",
            hint="Pass the endpoint a service answers at: upstream=signer.endpoint(8547).",
        )
    if not upstream.loopback:
        raise ValidationError(
            f"{kind}({port}): upstream {upstream.url} listens on {upstream.host}, so the "
            "ingress would not be the only path to it.",
            hint="Bind the upstream to 127.0.0.1 (listen='127.0.0.1:8080') and let the "
            "ingress be the way in.",
        )
    if upstream.scheme != "http":
        raise ValidationError(
            f"{kind}({port}): upstream {upstream.url} is not plain HTTP.",
            hint="The ingress terminates TLS; the upstream speaks HTTP on the loopback.",
        )


@dataclass(frozen=True, slots=True)
class AttestedTLS(Located):
    """The confidential ingress: TLS on *port*, relayed to *upstream* on the loopback.

    ``AttestedTLS(8546, upstream=signer.endpoint(8547))`` opens *port* in the firewall and nothing
    else: the upstream must listen on the loopback and expose no port of its own, so this
    is the only way requests reach it (the image refuses anything else).

    The image reserves the port, runs the relay (``socat``'s OpenSSL listener, as its own
    unprivileged user with only ``CAP_NET_BIND_SERVICE``), opens the firewall rule and
    records the ingress in the runtime manifest. A root oneshot makes the listener's key
    pair and self-signed certificate at every boot, on tmpfs only, and the relay reads
    them as credentials. The ingress brings the control plane: the attested endpoint on
    :8745 binds the certificate's key into every quote's user data, after the instance
    key, one part per port in port order, so a client that verified a quote can check the
    certificate it sees is the one that TD made (``tundravm attest INSTANCE --policy FILE
    --tls PORT``, or ``tools/cmd/attested-client`` in a relying party's own code). The
    certificate proves which TD terminates the connection, nothing about who connects:
    client authorisation stays the upstream's.
    """

    port: int
    _: KW_ONLY
    upstream: Endpoint

    def __post_init__(self) -> None:
        _check(self, self.port, self.upstream)

    @property
    def unit(self) -> str:
        """The relay's unit."""
        return f"attested-tls-{self.port}.service"

    def manifest(self) -> dict[str, object]:
        """The ingress as the runtime manifest records it."""
        return {
            "kind": "attested-tls",
            "port": self.port,
            "upstream": self.upstream.url,
            "unit": self.unit,
            "binding": "a key pair per boot; the attested endpoint's quote binds its key",
        }


@dataclass(frozen=True, slots=True)
class TLS(Located):
    """Ordinary HTTPS on *port*, relayed to *upstream*, with a *certificate* you deliver.

    *certificate* is a :class:`~tundravm.secrets.Secret` holding the PEM private key and
    certificate chain, delivered like any secret (``tundravm secrets deliver INSTANCE
    tls=@bundle.pem``): the key never enters the image. Nothing about it is attested;
    use :class:`AttestedTLS` when the client must know what it is talking to.
    """

    port: int
    _: KW_ONLY
    upstream: Endpoint
    certificate: Secret

    def __post_init__(self) -> None:
        _check(self, self.port, self.upstream)
        if not isinstance(self.certificate, Secret):
            raise ValidationError(
                f"TLS({self.port}): certificate {self.certificate!r} is not a Secret.",
                hint="Pass certificate=Secret('tls'): the key and chain are delivered, "
                "never built in.",
            )

    @property
    def unit(self) -> str:
        """The relay's unit."""
        return f"tls-{self.port}.service"

    def manifest(self) -> dict[str, object]:
        """The ingress as the runtime manifest records it."""
        return {
            "kind": "tls",
            "port": self.port,
            "upstream": self.upstream.url,
            "unit": self.unit,
            "certificate": self.certificate.name,
        }


type Ingress = AttestedTLS | TLS
"""A way in: :class:`AttestedTLS` or :class:`TLS`."""
