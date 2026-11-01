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
            hint="Pass the endpoint a component exports: upstream=raiko.http.",
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

    ``AttestedTLS(443, upstream=raiko.http)`` opens *port* in the firewall and nothing
    else: the upstream must listen on the loopback and expose no port of its own, so this
    is the only way requests reach it (the image refuses anything else). The listener
    generates its key pair inside the guest at every start and never stores it.

    What the image does today: reserve the port, run the relay (``socat``'s OpenSSL
    listener, as its own unprivileged user with only ``CAP_NET_BIND_SERVICE``), open the
    firewall rule, and record the ingress in the runtime manifest.

    What gate 3 adds (tools/, plan section 6.3): the attested endpoint on :8745
    binds the listener's public key into the quote's ``report_data``
    (``sha512(purpose ‖ nonce ‖ public key ‖ SSH host keys)``), so ``tundravm attest``
    hands the client a certificate it can verify against the operator's policy and a
    relayed quote cannot authenticate another connection. The client verifier policy,
    client authorisation and renewal are part of that contract. Until then the
    certificate is self-signed per boot and a client pins it on first contact, which
    ``attest`` reports as unverified.
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
            "binding": "self-signed per boot; quote binding is gate 3",
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
